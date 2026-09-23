# Tahap 3 (Feed ke Zep) — Desain Produksi

Status: **desain, belum implementasi** — dokumen ini menunggu review sebelum
Tahap 3 mulai dibangun. JANGAN mulai implementasi setelah dokumen ini selesai.

Konteks: [docs/investigations/tahap3_zep_feed_production.md](../investigations/tahap3_zep_feed_production.md)
(investigasi produksi, 2026-09-22) sudah menjawab 6 pertanyaan operasional dengan bukti
langsung (2 titik data nyata untuk skala batch, klasifikasi kegagalan Finnhub, konfirmasi
limit resmi Zep, dll). Dokumen ini mengambil temuan itu dan menjawab keputusan desain yang
masih terbuka di sana.

**Scope dokumen ini murni desain.** Tidak ada file baru/berubah di `backend/app/services/`
atau `backend/app/data_layer/`. Referensi yang dibaca (tidak diubah): `graph_builder.py`,
`app/utils/zep.py`, `news_data.py`, `universe.py`, `zep_entity_reader.py`. Semua nama
fungsi di bawah adalah **ilustratif** (menunjukkan bentuk/tanda tangan, bukan kode siap-tempel).

## Keputusan yang sudah diambil (given, tidak didesain ulang di sini)

- **Siklus hidup graph: create + delete PER RUN.** Bukan reuse lintas `as_of_date`.
  Kemampuan query lintas-waktu (filter `valid_at`/`invalid_at` dari investigasi §4.2)
  **ditunda**, bukan dibangun sekarang — trade-off yang disadari, bukan terlewat.
- **Kegagalan batch: FAIL-FAST.** 1 item gagal di sisi Zep (`status` bukan `succeeded`)
  = seluruh operasi Tahap 3 untuk `as_of_date` itu dianggap gagal. Tidak ada penerimaan
  sukses-sebagian.
- **Cakupan ticker: TANPA cap.** Semua ticker yang lolos `screen_universe()` dan berhasil
  di-fetch dikirim ke Zep — beda dari cap 10-ticker lama di `persona_generator.py`. Waktu
  tunggu bisa sampai puluhan menit-hitungan jam untuk universe besar; ini **diterima**.

---

## 1. Perbaikan timeout — WAJIB, prioritas tertinggi

**Formula:** timeout tidak boleh lagi konstanta tetap (`ZEP_INGESTION_WAIT_TIMEOUT_SECONDS=600`
apa adanya) — harus dihitung dari `item_count` memakai model investigasi §1.2:

```
model_wait(n)  = 476.9 + 1.527 × n                     # detik, model 2-titik-data
timeout(n)     = max(600, ceil(2 × model_wait(n)))     # margin 2x, floor 600s
```

**Kenapa margin 2× (multiplikatif), bukan model + buffer tetap (mis. +300 detik):**
investigasi eksplisit mencatat model ini cuma dari 2 titik data dan **bisa non-linear di
skala jauh lebih besar** (antrian internal Zep, throttling sisi-server yang tidak
teramati di 87-150 item). Buffer TETAP (mis. +300s) memberi proteksi yang sama besar
berapapun skala `n` — di `n`=1.500 item, model≈2.767s, +300s cuma menambah ~11% margin,
tidak cukup kalau perilaku ternyata super-linear di skala itu. Margin **multiplikatif**
menskalakan proteksinya bersama ketidakpastian yang juga membesar bersama skala — di
`n` besar, 2× model berarti kita masih punya toleransi sampai 2× lebih lambat dari yang
model prediksi sebelum benar-benar timeout. Floor 600s menjaga kompatibilitas ke belakang
untuk batch sangat kecil (di bawah baseline 87 item yang pernah diuji) supaya tidak lebih
agresif dari default lama untuk kasus kecil.

**Angka final untuk kasus terbesar (~1.500 item, universe penuh tanpa filter sektor):**

```
model_wait(1500) = 476,9 + 1,527 × 1500 = 2.767,4 detik (≈46,1 menit)
timeout(1500)    = max(600, ceil(2 × 2.767,4)) = 5.535 detik (≈92,25 menit / ≈1,54 jam)
```

Untuk skenario kecil (~44 ticker × 3 artikel efektif setelah retry ≈129 item, §"Estimasi
waktu" di bawah):

```
model_wait(129) = 476,9 + 1,527 × 129 = 673,9 detik
timeout(129)    = max(600, ceil(2 × 673,9)) = 1.348 detik (≈22,5 menit)
```

Ini dipakai sebagai parameter `timeout=` eksplisit ke `_wait_for_batch()` (yang sudah
menerima parameter ini, `graph_builder.py:631-636` — tidak perlu ubah signature, cukup
tidak lagi mengandalkan default). `item_count` yang dipakai untuk hitung formula ini adalah
jumlah item AKTUAL yang jadi dikirim ke `add_text_batches` (setelah fetch+retry+dedup di
§2, bukan jumlah ticker awal di `screen_universe()`).

---

## 2. Orkestrasi fetch berita seluruh ticker (tanpa cap), dengan retry

```
fetch_news_for_universe(tickers: List[str], as_of_date: Optional[str])
    -> Tuple[List[NewsItem], List[FailedTicker]]
```

**Alur (sekuensial, sesuai cara investigasi diuji — lihat "Di luar scope" di bawah untuk
kenapa paralelisasi TIDAK didesain di sini):**

1. Untuk SETIAP ticker di `tickers` (hasil `screen_universe()`, tanpa cap jumlah):
   panggil `get_news_data(ticker, as_of_date)` — percobaan pertama.
2. Kalau `success=False` (kegagalan jaringan/HTTP, bukan "0 artikel" — `get_news_data`
   sendiri sudah membedakan ini eksplisit lewat field `out_of_range`/`success`): **retry
   SATU KALI, LANGSUNG, tanpa delay/backoff tambahan** — panggil `get_news_data` sekali
   lagi persis dengan argumen yang sama.
   - **Kenapa tanpa backoff:** investigasi §2.2 mengukur 80% recovery HANYA dari retry
     langsung (timeout yang sama, 15 detik, tanpa jeda) — mekanismenya adalah ukuran
     respons yang besar untuk ticker bervolume-berita-tinggi, bukan rate-limit yang butuh
     didinginkan. Menambah backoff di sini hanya menambah waktu tanpa bukti perlu.
3. Ticker yang gagal di KEDUA percobaan → dicatat sebagai `FailedTicker(ticker, error)`,
   **TIDAK memblokir** ticker lain — lanjut ke ticker berikutnya.
4. Ticker dengan `success=True` (percobaan pertama ATAU kedua) → ambil artikelnya (cap
   3 artikel terbaru/ticker by `published_at` descending, dedup by `article_id` DALAM
   1 ticker — konvensi yang sama dipakai di kedua investigasi Zep sebelumnya).
5. **Dedup GLOBAL lintas semua ticker by `article_id`** (referensi konsep: dokumen Fase 9
   SUPERSEDED `fase9a_entity_extraction_schema.md` mencatat `article_id` "belum dipakai di
   mana pun" sebagai dedup key lintas-konsumen — Tahap 3 adalah pemakai pertama untuk
   tujuan ini; TIDAK ada kode Fase 9 yang diimpor, murni konsepnya). Pertahankan
   `seen_article_ids: Set[int]`; iterasi artikel per ticker sesuai urutan `tickers`, buang
   artikel yang `article_id`-nya sudah pernah masuk di ticker sebelumnya (kasus nyata:
   `NWSA`/`NWS`, dua kelas saham perusahaan yang sama, artikel identik — investigasi
   sebelumnya §1 lampiran).
6. Kembalikan `(news_items, failed_tickers)` — TIDAK ada exception dilempar dari sini
   sekalipun SEBAGIAN ticker gagal; hanya exception kalau argumen `tickers` sendiri kosong.

**`NewsItem` (bentuk data, bukan kelas nyata):** `ticker`, `headline`, `summary`,
`publisher`, `published_at` (ISO 8601, dari `NewsArticle.published_at` di `news_data.py`),
`article_id` (dedup key). `url` disimpan untuk audit tapi tidak perlu masuk teks episode
(lihat §3).

---

## 3. Format chunk teks ke Zep

`build_zep_batch(news_items)` **TIDAK menulis ulang** `GraphBuilderService.add_text_batches`
(`graph_builder.py:407-564`) — fungsi itu sudah menangani multi-`batch.add()` otomatis untuk
>350 item (investigasi §1.1, dikonfirmasi lewat pembacaan kode). Yang perlu didesain di sini
hanya: **bagaimana 1 `NewsItem` menjadi 1 elemen `chunks: List[str]`** yang diserahkan ke
`add_text_batches`.

**Format (sudah divalidasi 2× secara live, bukan tebakan):**

```
f"[{published_at}] {ticker}: {headline}. {summary}"
```

Konsisten dengan konvensi bracket-tanggal yang sudah ada di
`persona_generator._format_article_line` (`[tanggal] TICKER | publisher | headline |
summary`) — sedikit disederhanakan (tanpa `publisher`, karena investigasi tidak menemukan
`publisher` berguna untuk ekstraksi entity/edge Zep; boleh ditambahkan lagi di implementasi
kalau Tahap 4 butuh atribusi sumber).

**Kenapa `published_at` disertakan di TEKS (bukan cuma metadata):** `BatchAddItem` yang
dikonstruksi `add_text_batches` (`graph_builder.py:470-486`) **tidak meneruskan
`created_at`** — episode akan memakai waktu ingest sebagai anchor temporal Zep-nya sendiri,
BUKAN tanggal publikasi artikel sungguhan. Karena siklus hidup graph di Tahap 3 adalah
create+delete PER RUN (tidak ada akumulasi lintas hari untuk saat ini — lihat keputusan di
atas), ini bukan cacat kritis SEKARANG, tapi tetap layak dicatat: fakta yang diekstrak Zep
dari artikel lama (`published_at` beberapa minggu lalu) akan tercatat seolah baru terjadi
hari ini kalau hanya mengandalkan `created_at` bawaan Zep. Menyisipkan `published_at`
eksplisit di teks memberi LLM ekstraksi Zep sinyal tanggal yang benar untuk diekstrak jadi
atribut/fakta, walau tidak mengubah metadata `created_at` episode itu sendiri.

**Batas panjang:** limit 10.000 karakter/item (dikonfirmasi resmi, investigasi §1.1) jauh
di atas kebutuhan headline+summary manapun (rata-rata terukur 299-305 byte/item di kedua
run live) — **tidak perlu logika pemotongan tambahan**, `validate_batch_chunks` yang sudah
ada (`graph_builder.py:566-580`) sudah menolak item >10.000 karakter kalau suatu saat
terjadi (defense-in-depth yang sudah ada, tidak perlu diduplikasi di layer Tahap 3).

---

## 4. Ontology produksi — FIX, dengan syarat verifikasi

**Ontology final (sama seperti kedua run investigasi, TIDAK direvisi — sudah terbukti
bekerja 2× live, dan cukup untuk kebutuhan ekstraksi entity/edge yang diminta Tahap 4
sejauh ini):**

| Entity types | Atribut |
|---|---|
| `Company` | `ticker` |
| `Person` | — |
| `MarketEvent` | `event_type` |

| Edge types | Source → Target |
|---|---|
| `INVOLVES` | `MarketEvent` → `Company` |
| `COMMENTS_ON` | `Person` → `Company` |

3 entity type + 2 edge type = 5 total — jauh di bawah kedua interpretasi limit tier Flex
(10 gabungan ATAU 10/kategori, investigasi §3, ambiguitas belum terjawab).

**Prasyarat WAJIB sebelum dipakai produksi (bukan diasumsikan aman):** karena ambiguitas
limit di atas belum terjawab investigasi, dan ontology final BISA berubah di masa depan
kalau Tahap 4 minta entity type tambahan (mis. `Sector`, `AnalystRating`) — **sebelum
implementasi Tahap 3 di-deploy ke produksi, jalankan 1 uji kecil**: panggil
`set_ontology(graph_ids=[test_graph_id], ...)` dengan ontology sengaja mendekati batas
(mis. 10 entity type + 10 edge type = 20 total definisi kosong/dummy), amati apakah Zep
menolak di titik ke-10 gabungan atau menerima sampai 10 tiap kategori. Ini murni uji API
sekali jalan (biaya ~0, `set_ontology` tidak mengkonsumsi credit ingest), hasilnya
menentukan apakah ontology final di atas (5 total) punya ruang tumbuh sampai berapa entity
type tambahan sebelum implementasi Tahap 4 mengajukan penambahan.

`set_ontology()` dipanggil **SEKALI per run**, di awal `build_universe_graph()` (§5) —
karena siklus hidupnya create+delete per run (bukan reuse), **tidak ada risiko ontology
berubah di tengah akumulasi** yang jadi perhatian investigasi §3 untuk skenario reuse.

---

## 5. Siklus hidup graph — create + delete per run

```
build_universe_graph(as_of_date: Optional[str]) -> Dict[str, Any]
```

**Langkah, dalam SATU fungsi (encapsulated, lihat alasan kepemilikan cleanup di bawah):**

1. `tickers = screen_universe(sectors=..., as_of_date=as_of_date)` — tanpa cap.
2. `news_items, failed_tickers = fetch_news_for_universe(tickers, as_of_date)` (§2). Kalau
   `news_items` kosong (SEMUA ticker gagal fetch, bahkan setelah retry) → **return error,
   JANGAN panggil Zep sama sekali** (§8, titik kegagalan pertama).
3. `chunks = build_zep_batch(news_items)` (§3).
4. `graph_id = f"mirofish_universe_{as_of_date or 'live'}_{uuid4().hex[:8]}"` →
   `builder.create_graph(name, graph_id=graph_id)`.
5. `builder.set_ontology(graph_id, ONTOLOGY)` (§4, ontology final).
6. `timeout = compute_timeout(len(chunks))` (§1) → `submission =
   builder.add_text_batches(graph_id, chunks)` → `episode_uuids =
   builder._wait_for_batch(submission, timeout=timeout)`.
7. `filtered_entities = ZepEntityReader().filter_defined_entities(graph_id)` — **baca
   SEBELUM hapus** (lihat §7, ini yang mengoreksi kontradiksi Opsi A mentah di investigasi).
8. `builder.delete_graph(graph_id)` — **selalu dipanggil di blok `finally`**, baik langkah
   6-7 sukses maupun gagal (§6, §8).
9. Return kontrak sukses (§7) berisi `filtered_entities` + metadata audit.

**Siapa yang bertanggung jawab memanggil `delete_graph`: `build_universe_graph()` itu
sendiri, di blok `finally` internalnya — BUKAN Tahap 4/pemanggil di luar.** Alasan: karena
kontrak output ke Tahap 4 (§7) adalah `FilteredEntities` yang SUDAH diekstrak (bukan
`graph_id` mentah), Tahap 4 **tidak pernah menerima `graph_id` yang masih hidup** untuk
dioperasikan — tidak ada yang tersisa untuk dibersihkan pihak luar. Menaruh cleanup di
dalam fungsi menjaga siklus hidup graph sepenuhnya terenkapsulasi di Tahap 3, konsisten
dengan keputusan "create+delete per run" itu sendiri (satu fungsi, satu graph, dari lahir
sampai mati, tidak ada handle yang bocor ke luar).

---

## 6. Fail-fast pada batch partial/failed

Kalau `_wait_for_batch()` (langkah 6 di atas) melempar `RuntimeError` (status
`partial`/`failed`/`invalid`/`canceled` — `graph_builder.py:669-683`) ATAU `TimeoutError`
(timeout dari §1 terlampaui):

1. `graph_id` yang SUDAH dibuat di langkah 4 **tetap dihapus** (`delete_graph(graph_id)`
   di blok `finally`) — bukan soal "menerima yang sukses", murni supaya graph gagal tidak
   menumpuk sebagai sampah di project Zep (project ini juga dipakai untuk graph simulasi
   sosial lain, investigasi §3 — kebersihan project ini penting terlepas dari fitur apa
   yang gagal).
2. `build_universe_graph()` mengembalikan `{"success": False, "error": str(exc), ...metadata
   audit lain (lihat §7 untuk field yang tetap disertakan meski gagal)}`.
3. **TIDAK ADA data yang diteruskan ke Tahap 4** — tidak ada `filtered_entities` parsial,
   tidak ada `episode_uuids` sebagian. Ini konsisten dengan keputusan fail-fast: kalaupun
   secara teknis sebagian item berhasil diproses di sisi Zep sebelum status jadi `partial`,
   caller (Tahap 4) tidak pernah melihatnya karena graph-nya sudah dihapus di langkah 1.

---

## 7. Kontrak output ke Tahap 4 — koreksi terhadap Opsi A mentah di investigasi

Investigasi §5 memetakan dua opsi (A: `graph_id` saja, B: ringkasan/top-N). **Opsi A mentah
tidak bisa dipakai literal** dengan keputusan siklus hidup "create+delete per run" (§5 di
atas) — kalau Tahap 3 mengembalikan `graph_id` lalu langsung menghapusnya, Tahap 4 akan
menerima ID yang sudah tidak valid begitu ia sempat memanggil `ZepEntityReader` sendiri.
**Kontrak yang benar adalah VARIAN Opsi A**: Tahap 3 membaca entity SENDIRI sebelum hapus
(§5 langkah 7), lalu menyerahkan HASIL BACAAN itu (`FilteredEntities`, bukan `graph_id`)
ke Tahap 4. Ini bukan Opsi B (belum ada peringkasan/top-N/ranking apapun — itu tetap
keputusan terbuka investigasi §5 yang TIDAK diputuskan di sini, di luar scope dokumen
desain ini juga) — murni "Opsi A yang sudah menyesuaikan diri dengan siklus hidup yang
dipilih".

**Bentuk kontrak sukses:**

| Field | Isi | Kenapa disertakan |
|---|---|---|
| `success` | `True` | diskriminator standar |
| `filtered_entities` | `FilteredEntities.to_dict()` (`entities`, `entity_types`, `total_count`, `filtered_count`) | payload utama — bahan Tahap 4 |
| `item_count` | jumlah chunk yang dikirim ke Zep (setelah fetch+retry+dedup) | audit; dasar hitung `timeout` §1 |
| `failed_tickers` | `List[FailedTicker]` dari §2 (ticker+error, gagal di KEDUA percobaan) | Tahap 4/operator tahu ticker mana yang TIDAK punya representasi di graph — penting supaya "tidak ada entity untuk XYZ" tidak disalahartikan sebagai "tidak ada berita" |
| `ontology_used` | ontology final §4 (atau versi/hash-nya) | audit — kalau ontology berubah antar run, Tahap 4 bisa tahu skema mana yang berlaku untuk run ini |
| `ingest_seconds` | `batch_wait_seconds` aktual (bukan timeout budget) | observability — bangun basis data historis buat mengevaluasi/mengoreksi model §1 dari waktu ke waktu |
| `as_of_date` | echo dari argumen input | audit/traceability |

**Bentuk kontrak gagal (§6/§8):** `{"success": False, "error": str, "failed_tickers": [...],
"item_count": <jumlah item yang SEMPAT terkumpul sebelum gagal, 0 kalau gagal di fetch>,
"as_of_date": ...}` — `filtered_entities` TIDAK ADA sama sekali di bentuk ini (bukan `None`
atau list kosong — field ini memang tidak muncul, supaya konsumen yang lupa cek `success`
dulu akan error jelas `KeyError` alih-alih diam-diam memproses list kosong seolah itu hasil
valid).

---

## 8. Error handling menyeluruh — semua titik kegagalan

| # | Titik kegagalan | Sudah ada apa untuk dibersihkan? | Perilaku |
|---|---|---|---|
| 1 | Semua ticker gagal fetch (0 berhasil, bahkan setelah retry §2) | Tidak ada (belum panggil Zep) | Return error SEBELUM `create_graph` — tidak ada panggilan Zep sama sekali |
| 2 | `create_graph()` gagal (`graph_builder.py:218-261` — fungsi ini sendiri sudah retry+rekonsiliasi internal untuk error yang retryable; kalau tetap raise, berarti benar-benar gagal/tidak terkonfirmasi) | Tidak ada (graph tidak terkonfirmasi ada) | Return error, tidak ada `delete_graph` dipanggil |
| 3 | `set_ontology()` gagal — **catatan kejujuran:** ini beda dari #2 karena `create_graph` di langkah sebelumnya SUDAH sukses, jadi graph kosong (tanpa ontology) SUDAH ada di project Zep | Graph kosong SUDAH ada | **Refinement terhadap asumsi naif "belum ada apa-apa untuk dibersihkan":** tetap panggil `delete_graph(graph_id)` best-effort (bungkus try/except, error sekunder di sini cukup di-log bukan menimpa error utama) sebelum return error — supaya run yang gagal berulang tidak meninggalkan tumpukan graph kosong tanpa ontology di project |
| 4 | `add_text_batches()` gagal (unconfirmed submission — `graph_builder.py:443-539`, sudah punya rekonsiliasi sendiri, raise kalau tetap tidak bisa dipastikan) | Graph + ontology sudah ada | `delete_graph(graph_id)`, return error (§6 pola) |
| 5 | `_wait_for_batch()` raise `RuntimeError` (status `partial`/`failed`/`invalid`/`canceled`) | Graph + ontology + batch (sebagian) sudah ada | `delete_graph(graph_id)`, return error — TIDAK ada partial acceptance (§6) |
| 6 | `_wait_for_batch()` raise `TimeoutError` (timeout §1 terlampaui — bisa terjadi kalau model meleset di skala ekstrem, investigasi eksplisit tidak menjamin di atas 150 item) | Sama seperti #5 (batch mungkin masih diproses di sisi Zep, statusnya tidak diketahui saat timeout) | **PERLAKUAN SAMA seperti #5**: `delete_graph(graph_id)`, return error, tidak ada partial acceptance — konsisten fail-fast |
| 7 | `ZepEntityReader.filter_defined_entities()` gagal SETELAH batch sukses (mis. read exhausted retry, §6.2 investigasi — retry read sudah 3× built-in, ini kegagalan setelah itu habis) | Batch SUKSES penuh, tapi belum sempat dibaca | **Titik kegagalan tambahan yang tidak eksplisit diminta tapi konsekuensi langsung dari desain §5/§7 (baca-sebelum-hapus)** — `delete_graph(graph_id)` tetap dipanggil (konsisten §5: tidak ada `graph_id` hidup yang pernah diserahkan ke Tahap 4, jadi mempertahankan graph di sini hanya akan meninggalkannya sebagai sampah tak terjangkau), return error. **Trade-off yang disadari:** seluruh biaya ingest (bisa puluhan menit di skala besar, §1) hangus sia-sia kalau HANYA langkah baca-balik yang gagal padahal datanya sudah benar di sisi Zep — ini akibat langsung dari kombinasi "fail-fast" + "Tahap 4 tidak pernah pegang graph_id hidup" yang sudah diputuskan sebelumnya, dicatat di sini supaya bukan kejutan nanti |

---

## Diagram alur

```
screen_universe(sectors, as_of_date)
        |
        v
   tickers (tanpa cap)
        |
        v
fetch_news_for_universe(tickers, as_of_date)     [§2]
   - get_news_data per ticker, 1x retry langsung untuk yang gagal
   - dedup article_id GLOBAL lintas ticker
        |
        +---- SEMUA gagal? ----> return {success:false} SEBELUM Zep  [§8 #1]
        |
        v
   news_items (+ failed_tickers dicatat, tidak memblokir)
        |
        v
build_zep_batch(news_items) -> chunks: List[str]        [§3]
        |
        v
create_graph(graph_id)  ----gagal----> return error, no cleanup      [§8 #2]
        |
        v
set_ontology(graph_id, ONTOLOGY)  ----gagal----> delete_graph, return error   [§8 #3]
        |
        v
compute_timeout(len(chunks))                              [§1]
        |
        v
add_text_batches(graph_id, chunks)  ----gagal----> delete_graph, return error [§8 #4]
        |
        v
_wait_for_batch(submission, timeout=...)
        |
        +--- partial/failed/invalid/canceled ---> delete_graph, return error [§8 #5, §6]
        +--- TimeoutError ----------------------> delete_graph, return error [§8 #6]
        |
        v (succeeded)
ZepEntityReader().filter_defined_entities(graph_id)   ----gagal----> delete_graph, return error [§8 #7]
        |
        v
delete_graph(graph_id)   [SELALU, blok finally — sukses maupun gagal di titik manapun setelah create]
        |
        v
return {success:true, filtered_entities, item_count, failed_tickers,
        ontology_used, ingest_seconds, as_of_date}          [§7]
        |
        v
   ------> Tahap 4 (generate persona dari graph Zep)
```

---

## Estimasi waktu total — 2 skenario konkret

Model dan angka gagal-fetch memakai kombinasi dari kedua investigasi (`tahap3_zep_feed_production.md`
§1.2/§2.2 dan `zep_fase10_universe_scale_check.md` §1) — bukan pengukuran baru, murni
menggabungkan yang sudah terukur jadi 1 angka "total waktu Tahap 3" per skenario, dengan
asumsi eksplisit ditandai di mana pengukuran langsung tidak tersedia.

### Skenario kecil — 1 sektor, ~44 ticker (mis. Energy+Communication Services, seperti investigasi awal)

| Tahap | Waktu | Sumber |
|---|---|---|
| `screen_universe` (44 ticker, sudah tersaring sektor) | ≈25,1s | proporsional dari 286,8s/503 ticker × 44/503 (koreksi — bukan "<10s" seperti draf sebelumnya, itu keliru) |
| Fetch (44 ticker, first pass) | 301,6s (real, terukur langsung) | `zep_fase10_universe_scale_check.md` §1 |
| Retry utk ~7 ticker gagal pertama (asumsi ~10s/retry blended) | ~70s | diturunkan dari §2.2 investigasi ini (blend biaya retry sukses+timeout persisten) |
| **Subtotal fetch** | **≈372s (6,2 menit)** | |
| Item terkumpul (≈43/44 ticker efektif setelah retry × 3 artikel) | 129 item | 80% recovery rate §2.2 diterapkan ke 7 kegagalan awal |
| Ingest Zep (`model_wait(129)`, BUKAN timeout budget) | 673,9s (11,2 menit) | model §1 |
| Overhead (create+ontology+baca-balik+delete) | ~15s | diukur langsung 2× (create+ontology ~1s, baca-balik 5-6s untuk ratusan node, delete <1s) |
| **TOTAL skenario kecil** | **≈25,1 + 372 + 673,9 + 15 ≈ 1.086s ≈ 18,1 menit** | jumlah SEMUA baris di atas, termasuk `screen_universe` |

### Skenario besar — tanpa filter sektor, ~503 ticker (S&P 500 penuh)

| Tahap | Waktu | Sumber/asumsi |
|---|---|---|
| `screen_universe(sectors=None)` | 286,8s (4,8 menit, real terukur) | investigasi ini, live 2026-09-22 |
| Fetch first-pass, 160 ticker ter-besar (real diukur) | 1.326,1s | investigasi ini |
| Fetch first-pass, 343 ticker sisanya (asumsi ~6,5s/ticker — lebih cepat dari 160 ticker terbesar krn payload lebih kecil, mendekati rata-rata 6,85s/ticker sampel campuran-sektor sebelumnya) | ≈2.229,5s | **asumsi**, diturunkan dari §2.2 (bukan diukur langsung untuk 343 ticker sisa ini) |
| Retry: ~165 ticker gagal pertama (110 real + ~55 asumsi 16% dari 343 sisa) × ~10s/retry | ≈1.650s | blend §2.2 |
| **Subtotal fetch** | **≈5.206s ≈ 86,8 menit** | dominan dari total waktu — lihat catatan di bawah |
| Item terkumpul (≈470 ticker efektif setelah retry × 3 artikel) | ≈1.410-1.500 item | konsisten dengan estimasi "kasus terbesar ~1.500 item" §1 |
| Ingest Zep (`model_wait(1500)`) | 2.767,4s (46,1 menit) | model §1, angka sama seperti tabel investigasi §1.2 |
| Overhead (create+ontology+baca-balik ~3.200 node+delete) | ~65s | baca-balik diskalakan dari 6,0s@320 node (investigasi ini) ke ~3.200 node |
| **TOTAL skenario besar** | **286,8 + 5.206 + 2.767,4 + 65 ≈ 8.325s ≈ 138,8 menit ≈ 2,31 jam** | jumlah SEMUA baris di atas, termasuk `screen_universe` |

**Koreksi (dibanding draf sebelumnya dokumen ini):** total skenario besar sebelumnya salah
tertulis ≈8.038s/2,2 jam — itu hasil sebelumnya diam-diam TIDAK menjumlahkan baris
`screen_universe(sectors=None)` (286,8s) ke total, padahal baris itu sendiri tetap
dicantumkan di tabel dengan nilai penuh dan diagram alur menempatkan `screen_universe`
sebagai langkah pertama `build_universe_graph()` yang sama. Tidak ada alasan desain yang
sah untuk mengecualikannya di skenario besar (beda dengan skenario kecil, di mana baris
`screen_universe` SEKARANG sudah diperbaiki jadi ≈25,1s dan tetap disertakan — cuma
kontribusinya kecil secara absolut). Angka total yang benar untuk skenario besar adalah
**≈8.325s (≈138,8 menit ≈ 2,31 jam)**, bukan 2,2 jam.

**Catatan penting dari tabel di atas (implikasi desain, BUKAN keputusan baru di dokumen
ini):** untuk universe besar, **fase fetch (86,8 menit) mendominasi total waktu, LEBIH
BESAR dari fase ingest Zep (46,1 menit)** — kebalikan dari kesan awal investigasi yang
berfokus ke skala batch Zep. Fetch sekuensial (satu ticker per satu waktu) adalah
bottleneck utama di skala besar, bukan Zep. Ini **di luar scope dokumen ini untuk
diselesaikan** (task tidak meminta desain ulang konkurensi fetch) — dicatat eksplisit
sebagai kandidat optimasi paling jelas untuk iterasi desain berikutnya kalau ≈2,31 jam per
`as_of_date` dianggap terlalu lambat untuk backtest berulang.

---

## Di luar scope dokumen ini (untuk desain berikutnya, bukan diputuskan sekarang)

- **Paralelisasi fetch berita** — sekuensial di desain ini (mengikuti cara investigasi
  diuji); disebut eksplisit di atas sebagai bottleneck dominan di skala besar.
- **Peringkasan/ranking entity (top-N by degree/centrality)** — investigasi §5 mencatat
  ini belum ada kode-nya sama sekali; kontrak §7 di sini menyerahkan `FilteredEntities`
  MENTAH (semua entity, tanpa ranking) ke Tahap 4 — keputusan A-vs-B soal peringkasan
  tambahan itu sendiri tetap terbuka, di luar scope dokumen desain ini.
- **Reuse graph lintas `as_of_date` untuk backtest 252 hari** — sudah diputuskan DITUNDA
  (lihat "Keputusan yang sudah diambil" di atas), bukan hilang; kalau nanti diaktifkan,
  filter `valid_at`/`invalid_at` (investigasi §4.2) perlu di-wire ke `zep_paging`/
  `ZepEntityReader`, yang BELUM ada hari ini.
- **Verifikasi limit ontology tier Flex** (§4) — didesain SEBAGAI LANGKAH yang harus
  dilakukan sebelum produksi, tapi hasil ujinya sendiri belum ada (belum dijalankan).
