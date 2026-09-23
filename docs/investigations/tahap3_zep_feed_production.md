# Tahap 3 (Feed ke Zep) — kesiapan produksi untuk universe skala besar

Investigasi lanjutan dari [zep_viability_check.md](zep_viability_check.md) dan
[zep_fase10_universe_scale_check.md](zep_fase10_universe_scale_check.md), yang membuktikan Zep
hidup dan bagus di skala 44 ticker/87 item (satu kali pakai). Pertanyaan di sini murni
operasional: **kalau Tahap 3 dipanggil BERULANG untuk universe berbeda-beda ukuran — termasuk
screening tanpa filter sektor yang bisa menghasilkan ratusan ticker — apa yang nyata-nyata
terjadi, dan apa yang harus diputuskan di desain nanti?**

**Investigasi murni — tidak ada kode produksi diubah, tidak ada file baru di
`backend/app/services/` atau `backend/app/data_layer/`.** Semua angka di bawah adalah hasil
menjalankan kode produksi asli (`screen_universe`, `get_news_data`, `GraphBuilderService`,
`ZepEntityReader`) tanpa modifikasi, terhadap Zep Cloud dan Finnhub sungguhan, lewat dua skrip
sekali-pakai di scratchpad sesi (tidak di-commit, dihapus setelah investigasi). Graph uji dibuat
lalu dihapus di blok `finally` — tidak ada sampah tertinggal di project Zep.

## Ringkasan jawaban

| # | Pertanyaan | Jawaban terukur |
|---|---|---|
| 1 | Batas Zep Batch API & skala waktu | **350 item/panggilan `batch.add`, 50.000 item/batch, 10.000 karakter/item — dikonfirmasi dari dokumentasi resmi Zep DAN sudah persis jadi konstanta di kode produksi (`graph_builder.py`).** Kode produksi SUDAH menangani >350 item lewat multiple `batch.add()` calls di bawah 1 `batch_id`. Dua titik data nyata (87 & 150 item) menunjukkan ada **overhead tetap ~477 detik + ~1,5 detik/item marginal** — BUKAN proporsional murni seperti diasumsikan investigasi sebelumnya dari 1 titik data |
| 2 | Kegagalan fetch Finnhub | **`get_news_data` benar-benar TANPA retry sama sekali (dikonfirmasi di kode+docstring).** Temuan BARU: tingkat kegagalan **sangat tidak acak** — berkorelasi kuat dengan volume berita (ticker mega-cap/populer paling sering timeout, BUKAN ticker kecil), dan **80% dari kegagalan itu SEMBUH hanya dengan satu kali retry** (bukan kegagalan permanen) |
| 3 | Ontology untuk produksi | `set_ontology(graph_ids=[...])` **ter-scope per graph_id** (dikonfirmasi dari dokumentasi resmi) dan **tidak additive** dalam satu scope — aman dipanggil ulang tiap run selama definisinya sama. Tier Flex project ini dibatasi **10 custom entity & edge types**; konstanta kode `MAX_ONTOLOGY_TYPES=10` diterapkan TERPISAH untuk entity DAN edge (berpotensi sampai 20 total) — **ambiguitas yang belum terjawab (lihat §Keterbatasan)** |
| 4 | Siklus hidup graph | **REUSE 1 graph_id lintas as_of_date, bukan create+delete per run** — didukung bukti kuat: kode produksi (`ZepGraphMemoryUpdater`) SUDAH memakai pola ini, dan Zep SDK punya `SearchFilters.valid_at/invalid_at/expired_at` untuk baca balik point-in-time dari graph yang terakumulasi. Overhead create+delete kecil (~1 detik gabungan, diukur langsung 2×) tapi REUSE tetap lebih baik untuk backtest 252 hari — lihat §4 untuk angka |
| 5 | Kontrak output ke Tahap 4 | `ZepEntityReader.filter_defined_entities()` mengembalikan SEMUA entity terfilter tanpa ranking — **tidak ada kode top-N/centrality di mana pun di codebase** (dikonfirmasi grep). Tahap 4 HARUS memutuskan sendiri apakah menerima `FilteredEntities.to_dict()` mentah atau butuh peringkasan tambahan — ini keputusan desain yang belum ada jawabannya di kode manapun sekarang |
| 6 | Error handling Zep sendiri | `_wait_for_batch` memperlakukan status `partial`/`failed`/`invalid`/`canceled` SAMA — **selalu hard-fail (raise), termasuk `partial`** — tidak ada penerimaan sukses-sebagian. Retry HANYA untuk *read* (408/429/5xx/timeout, 3x, exponential backoff); *write* (batch.create/add/process) sengaja TIDAK di-retry otomatis (ada mekanisme rekonsiliasi manual). **Temuan kritis: default `ZEP_INGESTION_WAIT_TIMEOUT_SECONDS=600` sudah TERLAMPAUI oleh KEDUA titik data nyata (609,7s dan 705,9s)** — di produksi tanpa override, ingest akan gagal karena timeout jauh sebelum mencapai skala ratusan ticker |

---

## 1. Skala & waktu — titik data KEDUA, bukan ekstrapolasi dari 1 titik

### 1.1 Batas resmi Zep — dikonfirmasi dari dokumentasi, bukan diasumsikan

Dicek langsung dari `help.getzep.com/adding-batch-data` (bukan dikutip dari sumber lain):

> "Each call to `batch.add` accepts up to **350 items**." / "A single batch can contain up to
> **50,000 items**." / episode data "Subject to the same **10,000-character limit** as
> `graph.add`." / "To ingest more than 350 items, make multiple `batch.add` calls against the
> same batch ID before calling `batch.process`."

**Ini PERSIS konstanta yang sudah ada di `GraphBuilderService.validate_batch_chunks`**
(`graph_builder.py:566-580`): `batch_size` 1-350, total chunks ≤50.000, item ≤10.000 karakter.
Bukan kebetulan — kode ini SUDAH ditulis sesuai limit resmi.

**Lebih penting: kode produksi SUDAH menangani multi-batch-add.** `add_text_batches`
(`graph_builder.py:407-564`) melakukan `for i in range(0, total_chunks, batch_size)` — kalau
`chunks` > 350, ia otomatis memanggil `batch.add()` BERKALI-KALI di bawah SATU `batch_id` (dari
SATU `batch.create()`), baru memanggil `batch.process()` sekali di akhir. Untuk universe 900+
item (300 ticker × 3 artikel), ini berarti **3 kali panggilan `batch.add()`** (350+350+200),
bukan 3 batch terpisah — sudah tervalidasi lewat pembacaan kode, bukan asumsi. **Catatan
kejujuran:** jalur multi-`batch.add()` ini TIDAK diuji langsung secara live di investigasi ini
(butuh >350 item nyata, terlalu mahal waktu untuk sesi ini) — diverifikasi lewat pembacaan kode
saja, ditandai di §Keterbatasan.

### 1.2 Dua titik data nyata — bukan lagi ekstrapolasi dari 1 titik

| Run | Item | Total byte | Byte/item | Submit | Wait (`_wait_for_batch`) | Detik/item |
|---|---|---|---|---|---|---|
| Investigasi sebelumnya (87 item) | 87 | 26.520 | 304,8 | 1,2s | **609,7s** | 7,01 |
| Investigasi INI (150 item, live 2026-09-22) | 150 | 44.855 | 299,0 | 1,7s | **705,9s** | 4,71 |

Byte/item hampir identik (304,8 vs 299,0) — kedua run sebanding, aman dibandingkan apples-to-
apples. **Detik/item TURUN saat skala naik** (7,01 → 4,71) — ini BUKAN error pengukuran, ini
sinyal nyata bahwa ada **overhead tetap per batch**, persis pertanyaan yang diajukan di brief.

Fit linear dari 2 titik (`wait = a + b × item_count`):

```
b = (705,9 - 609,7) / (150 - 87) = 96,2 / 63 = 1,527 detik/item (marginal)
a = 609,7 - 87 × 1,527 = 476,9 detik (overhead tetap per batch)
```

**Implikasi untuk universe besar (model, bukan pengukuran langsung di atas 150 item):**

| Skenario | Item | Estimasi wait (model) | vs. ekstrapolasi naif (7,01s/item dari 1 titik data lama) |
|---|---|---|---|
| 44 ticker × 3 (baseline lama) | 87 | 609,7s (aktual) | — |
| 50 ticker × 3 (baseline baru) | 150 | 705,9s (aktual) | — |
| 116 ticker × 3 (1 `batch.add` call, di bawah limit 350) | 350 | ≈1.011s (16,9 menit) | 2.454s (naif 3,5× lebih pesimis) |
| 300 ticker × 3 (butuh 3× `batch.add()`) | 900 | ≈1.851s (30,9 menit) | 6.309s (naif 3,4× lebih pesimis) |
| Universe penuh tanpa filter, semua fetch sukses × 3 art. | 1.500 | ≈2.767s (46,1 menit) | 10.515s (naif 3,8× lebih pesimis) |

**Catatan kejujuran soal model ini:** ini fit garis lurus dari TEPAT 2 titik data nyata — cukup
untuk menjawab pertanyaan arah ("apakah ada overhead tetap": **ya, terbukti nyata, bukan
proporsional murni**), tapi TIDAK cukup untuk memastikan bentuk kurva di atas 150 item (Zep bisa
saja punya perilaku non-linear lain di skala jauh lebih besar — antrian internal, throttling
sisi-server, dll — yang tidak akan terlihat dari 2 titik). Model ini adalah rekomendasi
estimasi terbaik yang tersedia sekarang, BUKAN jaminan.

### 1.3 Temuan kritis yang tidak diminta tapi ditemukan: timeout default sudah tidak cukup

`ZEP_INGESTION_WAIT_TIMEOUT_SECONDS = 600` (`app/utils/zep.py:28`) adalah timeout DEFAULT yang
dipakai `_wait_for_batch` kalau caller tidak override secara eksplisit (jalur produksi
`_build_graph_worker` TIDAK override ini). **Kedua titik data nyata di atas (609,7s dan 705,9s)
SUDAH MELEBIHI 600 detik ini** — kalau investigasi ini/investigasi sebelumnya memanggil
`_wait_for_batch` tanpa override timeout eksplisit, keduanya akan gagal dengan `TimeoutError`
sebelum sempat `succeeded`. (Skrip investigasi ini secara sengaja memberi `timeout=3600` — kalau
tidak, run 150-item ini sendiri sudah akan timeout.)

**Ini bukan masalah hipotetis untuk skala ratusan ticker — ini SUDAH jadi masalah nyata di
skala 87-150 item, jauh di bawah skala yang diminta brief.** Desain Tahap 3 HARUS eksplisit
menaikkan/mem-parameterisasi timeout ini berdasarkan `item_count` (mis. pakai model §1.2 + margin
aman), bukan mengandalkan default 600 detik.

---

## 2. Kegagalan fetch Finnhub — bukan cuma "berapa persen", tapi "yang mana"

### 2.1 Tidak ada retry sama sekali — dikonfirmasi di kode

`fetch_news_data`/`get_news_data` (`news_data.py`) — dikutip langsung dari docstring dan kode:

```python
# ... Kegagalan Finnhub SUNGGUHAN (network error / 429 / status non-200 / JSON tidak valid /
# API key kosong) mengembalikan success=False, error=<pesan>, TIDAK di-retry otomatis
# di sini — pemanggil (orkestrasi batch Fase 8c) yang bertanggung jawab atas
# retry/throttling.
```
(`news_data.py:143-149`)

```python
except requests.exceptions.RequestException as e:
    logger.warning(f"Fetch berita {ticker} gagal (kesalahan jaringan): {e}")
    return _news_error(ticker, f"Permintaan ke Finnhub gagal: {e}", as_of_date)
```
(`news_data.py:211-213`)

```python
if resp.status_code == 429:
    # JANGAN retry otomatis di sini — bisa memperparah kalau dipanggil dalam
    # batch besar ...
```
(`news_data.py:215-225`)

**Ini keputusan desain SENGAJA, bukan kelalaian** — retry didelegasikan ke orkestrasi pemanggil,
yang belum dibangun di manapun (Tahap 3 belum ada).

### 2.2 Temuan baru: kegagalan berkorelasi dengan volume berita, TIDAK acak

Run live hari ini: `screen_universe(sectors=None)` → **503 ticker** (mengkonfirmasi langsung
klaim brief "berpotensi ratusan ticker" — S&P 500 penuh tanpa filter sektor menghasilkan 503
kandidat sebelum filter market-cap lain). Karena `screen_universe` mengurutkan hasil **menurun
berdasar market cap**, 160 ticker pertama yang di-fetch (untuk menghemat waktu sesi) didominasi
mega/large-cap: `NVDA, GOOG, MSFT, AMZN, META, TSLA, AVGO, ...`.

**Hasil pertama (tanpa retry):** 50/160 sukses (31,25%), **110/160 gagal (68,75%)** — jauh lebih
buruk dari 16% di investigasi sebelumnya (universe Energy+Communication Services, sektor
campuran, bukan ticker terbesar S&P 500 secara global).

**Diagnosis lanjutan (memanggil ulang `get_news_data` PERSIS pada 110 ticker yang gagal itu,
Finnhub sungguhan lagi):**

| Kategori | Jumlah | Bukti |
|---|---|---|
| Sukses saat di-retry (murni transient) | **88/110 (80%)** | NVDA 249 artikel, DELL 247, PG 233, UNH 225, GE 247, NFLX 246, dst — **artikel SANGAT BANYAK**, bukan ticker sepi berita |
| Gagal lagi, error jaringan sungguhan (persisten) | **22/110 (20%)** | `HTTPSConnectionPool: Read timed out (read timeout=15)` — konsisten, bukan acak |
| "Berhasil" tapi 0 artikel (bukan kegagalan jaringan) | **0/110 (0%)** | Tidak ada satupun — mengkonfirmasi tidak ada masalah "ticker sepi berita" di sampel ini |

**Kesimpulan konkret:** kegagalan didorong oleh **ukuran respons Finnhub yang besar untuk ticker
bervolume-berita-tinggi** (200-250 artikel dalam window 90 hari), yang secara marginal melebihi
timeout `requests.get(..., timeout=15)` yang tetap (`news_data.py:209`) — BUKAN rate-limit acak,
BUKAN ticker yang genuinely tidak punya data. Dengan kata lain: **pipeline yang mengambil sampel
dari ticker paling besar/paling banyak diberitakan (persis pola `_select_tickers` yang sudah
ada di `persona_generator.py`, sort-by-market-cap) akan secara SISTEMATIS kena tingkat kegagalan
lebih tinggi justru pada ticker yang paling penting untuk dicakup** — pola yang lebih buruk dari
sekadar "16-20% acak" yang diasumsikan investigasi sebelumnya.

**Implikasi langsung untuk cakupan (jawaban literal pertanyaan brief):** TANPA retry apapun,
gagal fetch bisa memangkas cakupan dari 100% jadi ~31% di universe yang didominasi mega-cap
(kasus terburuk terukur) sampai ~84% di universe campuran sektor (kasus investigasi sebelumnya).
**DENGAN satu kali retry** (percobaan kedua, timeout sama 15 detik, tanpa backoff sekalipun),
cakupan pulih ke **(50+88)/160 = 86,25%** — bahkan LEBIH BAIK dari baseline 84% sebelumnya.
**Satu retry sederhana adalah perbaikan dengan leverage sangat tinggi**, jauh lebih murah
daripada menaikkan timeout `requests.get` (yang punya trade-off memperlambat SEMUA request, bukan
cuma yang gagal).

---

## 3. Ontology untuk produksi — reuse aman, tapi ada batas tier yang perlu diperjelas

Dikonfirmasi dari dokumentasi resmi Zep (`help.getzep.com/customizing-graph-structure`):

> "When these parameters [`graph_ids`] are provided, the ontology will only apply to the
> specified users and graphs, while other users and graphs in the project will continue using
> the previously set ontology" — **ontology ter-scope per graph_id**, bukan project-wide.
>
> "The `set_ontology` method overwrites any previously defined custom entity and edge types...
> the set of custom entity and edge types is always the list of types provided in the last
> `set_ontology` method call" — **tidak additive** DALAM satu scope (graph_id yang sama).
>
> "ontologies affect only data processed after they are set; existing data is not
> re-extracted automatically" — **mengubah ontology di tengah jalan TIDAK mengklasifikasi ulang
> episode yang sudah masuk sebelumnya.**

`GraphBuilderService.set_ontology` (`graph_builder.py:399-405`) sudah memanggil
`graph.set_ontology(graph_ids=[graph_id], ...)` — sudah benar ter-scope per graph, bukan
menimpa ontology graph lain di project yang sama (relevan karena project Zep ini juga dipakai
untuk graph simulasi sosial terpisah, lihat [zep_viability_check.md](zep_viability_check.md) §1).

**Implikasi untuk desain:** aman memanggil `set_ontology(graph_id, ONTOLOGY)` di SETIAP run
(baik reuse 1 graph_id ataupun buat baru per run) selama definisi ontology-nya IDENTIK setiap
kali — panggilan berulang dengan definisi sama = no-op efektif, bukan bug. Yang TIDAK aman:
mengubah bentuk ontology di tengah siklus hidup graph yang sedang diakumulasi (mis. menambah
entity type baru di hari ke-100 dari 252 hari backtest) — 99 hari episode sebelumnya TIDAK akan
diekstrak ulang dengan skema baru.

**Batas jumlah — dikonfirmasi dari halaman pricing resmi (`getzep.com/pricing`):**

| Tier | Custom entity & edge types |
|---|---|
| Free | 5 |
| **Flex (tier project ini, dari `zep_fase10_universe_scale_check.md` §4)** | **10** |
| Flex Plus / Enterprise | 20 |

**Ambiguitas yang belum terjawab (lihat §Keterbatasan):** halaman pricing menulis "10 custom
entity & edge types" tanpa menjelaskan apakah itu 10 GABUNGAN (entity+edge) atau 10 PER
kategori. Kode `graph_builder.py:332,358` menerapkan `[:MAX_ONTOLOGY_TYPES]` (=10) **terpisah**
untuk `entity_types` dan `edge_types` — asumsi implisit di kode adalah "10 entity + 10 edge = 20
total diperbolehkan", yang BISA SALAH kalau tier Flex sebenarnya membatasi 10 gabungan. Investigasi
ini tidak menguji ini secara live (ontology 3 entity + 2 edge di kedua run cukup kecil, tidak
pernah mendekati limit manapun) — perlu diklarifikasi langsung dengan Zep atau diuji dengan
ontology besar sebelum desain Tahap 3 menetapkan ontology produksi final.

---

## 4. Siklus hidup graph — REKOMENDASI: reuse 1 graph_id, bukan create+delete per run

### 4.1 Preseden kuat sudah ada di codebase produksi

`ZepGraphMemoryUpdater` (`zep_graph_memory_updater.py`) — dipakai untuk memory graph simulasi
sosial — **SUDAH memakai pola reuse**: 1 `graph_id` dibuat sekali per simulasi
(`ZepGraphMemoryManager.create_updater`), lalu dipakai berulang kali sepanjang seluruh durasi
simulasi (banyak "putaran" waktu, tiap kali memanggil `client.graph.add()` — bukan `create_graph`
baru — untuk menambah episode baru ke graph yang SAMA). Tidak ada `delete_graph` otomatis di
akhir siklus normal updater ini. Ini preseden LANGSUNG untuk pola "akumulasi lintas waktu dalam
1 graph_id", walau domainnya beda (aktivitas simulasi, bukan berita harian).

### 4.2 Filter waktu untuk baca balik — sudah ADA di SDK, belum dipakai `ZepEntityReader`

Dikonfirmasi langsung dari source SDK terpasang (`zep_cloud/types/search_filters.py`):
`SearchFilters` punya field `valid_at`, `invalid_at`, `expired_at`, `created_at` — masing-masing
array `DateFilter` 2D (OR di luar, AND di dalam), bisa dipakai lewat parameter `filters=` di
`client.graph.node.get_by_graph_id()`/`client.graph.edge.get_by_graph_id()`, dan
`search_filters=` di `client.graph.search()`. Ini PERSIS mekanisme yang dibutuhkan untuk
"baca 1 as_of_date tertentu dari graph yang isinya akumulasi banyak hari": filter edge dengan
`valid_at <= as_of_date` DAN (`invalid_at` kosong ATAU `invalid_at > as_of_date`) untuk
merekonstruksi state graph "seolah-olah pada tanggal X" — mekanisme bi-temporal invalidation
Zep/Graphiti asli, bukan sesuatu yang perlu dibangun dari nol.

**Catatan penting:** `zep_paging.fetch_all_nodes`/`fetch_all_edges` (dipakai `ZepEntityReader`
dan `GraphBuilderService`) **BELUM meneruskan parameter `filters` ini** — kapabilitasnya ADA di
level SDK/API, tapi belum di-wire ke fungsi yang dipanggil `ZepEntityReader.filter_defined_entities()`
sekarang. Ini pekerjaan desain/implementasi Tahap 3, bukan keterbatasan Zep.

Bukti tambahan bahwa Zep memang didesain untuk pola ini: skrip validasi manual yang sudah ada di
repo (`backend/scripts/validate_zep_cloud_integration.py`) secara eksplisit menguji **temporal
invalidation** dalam 1 `graph_id` yang sama — menambah episode baru lalu memverifikasi edge lama
ter-invalidate (`invalid_at` terisi) via `graph.add()` berturut-turut, bukan `graph.create()`
baru tiap kali.

### 4.3 Overhead create+delete — diukur langsung, kecil

Dari kedua run live (investigasi sebelumnya dan hari ini): `create_graph()` + `set_ontology()`
selesai dalam **~1 detik gabungan** (log timestamp `21:01:18`→`21:01:19` untuk 150-item run);
`delete_graph()` juga **<1 detik** (selesai di detik yang sama dengan baris log berikutnya).
**Overhead create+delete BUKAN faktor pembatas** — kalau nanti dipilih pola "1 graph per run",
biayanya cuma ~1-2 detik/run, bukan menit.

### 4.4 Rekomendasi eksplisit — dengan angka untuk backtest 252 hari

**Reuse 1 graph_id lintas seluruh backtest (252 as_of_date), bukan create+delete per hari.**

Alasan biaya/waktu konkret:
1. **Overhead create+delete per-run KECIL secara absolut (~1-2 detik)**, tapi kalau memang mau
   1 graph per hari untuk 252 hari, itu tetap 252×(1-2 detik) ≈ 4-8 menit TOTAL overhead murni
   administratif — kecil dibanding total waktu ingest (§1.2), jadi **overhead create/delete
   sendiri BUKAN alasan kuat untuk reuse** (ini mengoreksi asumsi implisit yang mungkin muncul
   dari harga rendah di §4.3 — biayanya memang rendah untuk KEDUA pilihan, jadi keputusan harus
   didasarkan pada alasan lain, bukan biaya create/delete).
2. **Alasan sesungguhnya untuk reuse: kebutuhan BACA BALIK, bukan biaya tulis.** Kalau
   Tahap 4/5/7 (persona, adapter, ekstraksi sinyal) perlu MEMBANDINGKAN entity/fakta ANTAR
   as_of_date (mis. "bagaimana sentimen terhadap AAPL berubah dari hari ke-50 ke hari ke-100"),
   itu HANYA mungkin lewat 1 graph yang terakumulasi + filter `valid_at`/`invalid_at` (§4.2) —
   252 graph terpisah TIDAK BISA menjawab pertanyaan lintas-waktu semacam itu sama sekali, karena
   tiap graph terisolasi total dari yang lain.
3. **252 graph terpisah = 252× `set_ontology()` call terpisah** (masing-masing scoped ke
   graph_id berbeda) — bukan masalah biaya (tiap call ~1 detik), tapi menambah 252 titik potensi
   kegagalan administratif (create gagal, ontology gagal ter-set, dll — tiap kegagalan di titik
   manapun butuh reconciliation terpisah, lihat kode `_find_batch_by_operation_id` dkk yang sudah
   ada). 1 graph_id = 1 kali setup, sisanya murni `add_text_batches` berulang.
4. **Risiko reuse yang HARUS ditangani di desain (bukan gratis):** ontology harus FIX dari awal
   (§3 — tidak boleh berubah bentuk di tengah 252 hari), dan `ZepEntityReader` HARUS di-extend
   untuk meneruskan `filters` bertingkat waktu (§4.2, saat ini belum ada) — tanpa itu, reuse
   1 graph_id tanpa filter waktu saat baca balik akan salah-menganggap fakta hari ke-1 masih
   berlaku persis sama di hari ke-252 tanpa pembedaan, KECUALI mengandalkan `filter_defined_entities()`
   memang mengambil TOTAL state (union semua entity yang PERNAH ada, bukan snapshot 1 hari) —
   ini justru celah nyata yang harus ditutup sebelum reuse dipakai untuk backtest.

**Ringkas: reuse graph_id direkomendasikan KARENA nilai bacanya (query lintas waktu yang secara
struktural tidak mungkin didapat dari graph terpisah), BUKAN karena penghematan biaya create/
delete (yang sama-sama murah untuk kedua opsi) — tapi rekomendasi ini bersyarat pada Tahap 3/4
membangun filter `valid_at` di `ZepEntityReader`, yang belum ada hari ini.**

---

## 5. Kontrak output ke Tahap 4 — belum ada jawaban di kode, ini keputusan terbuka

`ZepEntityReader.filter_defined_entities()` (dipakai berulang di kedua run live investigasi ini
dan sebelumnya) mengembalikan `FilteredEntities(entities: List[EntityNode], entity_types: Set[str],
total_count: int, filtered_count: int)` — **daftar LENGKAP tanpa urutan/ranking apapun** (urutan
mengikuti urutan iterasi `all_nodes`, bukan relevansi). Run hari ini: 320 node total, 202
terfilter (Company/MarketEvent/Person) dari 150 item — makin besar universe, makin besar pula
daftar mentah ini secara proporsional (§1.2's linear-ish scaling berlaku juga di sisi ekstraksi:
150→202 filtered vs 87→112 filtered, rasio 1,72× item → 1,80× entity, konsisten).

**Dikonfirmasi lewat grep menyeluruh (`centrality`, `degree_count`, `top_n`, `top-N`,
`sort.*edges` di `backend/app/services/` dan `backend/app/data_layer/`): TIDAK ADA kode
peringkasan/ranking entity di manapun di codebase ini.** "Top-N by centrality" yang disebut di
`zep_fase10_universe_scale_check.md` §3 sebagai "nilai tambah unik Zep" adalah ide yang
DIJELASKAN, bukan fitur yang ADA — investigasi ini mengkonfirmasi ulang itu masih benar hari ini.

**Implikasi untuk Tahap 3→4 (bukan keputusan, murni pemetaan opsi):**
- **Opsi A (kontrak minimal):** Tahap 3 hanya mengembalikan `graph_id` (+ metadata run: item
  count, ontology yang dipakai, waktu ingest). Tahap 4 memanggil `ZepEntityReader` sendiri,
  penuh tanggung jawab menangani ratusan entity mentah (202 dari 150 item — untuk universe
  900 item, bisa ~1.200+ entity mentah berdasar rasio yang sama).
- **Opsi B (kontrak diringkas):** Tahap 3 sendiri memanggil `filter_defined_entities()` lalu
  meringkas (top-N by degree, atau filter by entity type tertentu) sebelum diserahkan ke Tahap 4
  — TAPI logika peringkasan ini **belum ada sama sekali**, harus dibangun dari nol, dan definisi
  "degree"/"centrality" perlu dihitung manual dari `related_edges` per `EntityNode` (data
  mentahnya sudah ada di `FilteredEntities`, tinggal `len(entity.related_edges)` per entity —
  bisa dihitung, tapi belum ditulis).

Investigasi ini TIDAK memutuskan A vs B (di luar scope investigasi) — hanya memastikan: apapun
yang dipilih, itu kode BARU yang harus ditulis di fase desain, bukan sesuatu yang bisa "langsung
pakai" dari `ZepEntityReader` hari ini.

---

## 6. Error handling menyeluruh — kegagalan Zep sendiri

### 6.1 Kegagalan batch sebagian (`partial`) — hard-fail, tidak ada penerimaan sukses-sebagian

`_wait_for_batch` (`graph_builder.py:631-718`):

```python
terminal_states = {"succeeded", "partial", "failed", "invalid", "canceled"}
...
if status != "succeeded":
    failed_items = [item for item in items if getattr(item, "status", None) not in {"succeeded", "skipped"}]
    first_error = getattr(failed_items[0], "error", None) if failed_items else None
    raise RuntimeError(
        f"Zep batch {submission.batch_id} ended as {status}; "
        f"failed_items={len(failed_items)}; first_error={first_error}"
    )
```

**Zep API sendiri MEMBEDAKAN `partial` (sebagian sukses) dari `failed` (semua gagal)** — tapi
kode produksi saat ini memperlakukan KEDUANYA identik: raise, seluruh operasi dianggap gagal,
TIDAK ada jalur untuk menerima 899 dari 900 item yang berhasil dan hanya menandai 1 item yang
gagal. Untuk universe besar (900+ item), kemungkinan minimal 1 item gagal (mis. konten yang
memicu error ekstraksi Zep) naik seiring jumlah item — desain Tahap 3 perlu memutuskan apakah
ini diterima (fail-fast, aman tapi boros — 1 item gagal membuang seluruh 899 yang sukses secara
LOGIKA CALLER, walau datanya kemungkinan tetap tersimpan di sisi Zep) atau perlu ditulis ulang
untuk menerima `partial` dengan daftar item gagal eksplisit.

### 6.2 Retry read vs write — kebijakan sudah matang, sudah diverifikasi lewat pemakaian nyata

`is_retryable_zep_error`/`call_zep_read_with_retry` (`app/utils/zep.py:92-162`) — dipakai
langsung di kedua run investigasi ini (setiap polling `batch.get` selama ratusan detik):
retry HANYA untuk error transport (`httpx.TimeoutException`/`TransportError`,
`ConnectionError`/`TimeoutError`/`OSError`) dan status 408/429/5xx, maksimal 3 percobaan,
exponential backoff (dihormati `Retry-After` header kalau ada), dibatasi 60 detik/percobaan.
Ini HANYA untuk operasi *read* (`batch.get`, `graph.node.get_by_graph_id`, dst).

*Write* (`batch.create`/`batch.add`/`batch.process`/`graph.create`) **sengaja TIDAK di-retry
otomatis** — kode punya komentar eksplisit alasannya: "create/add are not documented as
idempotent, and an ambiguous replay can duplicate graph episodes." Sebagai gantinya, ada
mekanisme rekonsiliasi (`_find_batch_by_operation_id`, `_reconcile_batch_item_count`) yang
mem-verifikasi state SEBENARNYA di server (via operation_id yang disimpan di metadata) alih-alih
mengulang POST yang ambigu. Ini desain yang SUDAH matang — tidak ditemukan celah di dalamnya
selama investigasi ini.

### 6.3 Timeout Zep sendiri — dua timeout terpisah, satu di antaranya sudah terbukti tidak cukup

- `ZEP_HTTP_REQUEST_TIMEOUT_SECONDS = 60.0` — timeout per-request HTTP (httpx client level),
  terpisah dari:
- `ZEP_INGESTION_WAIT_TIMEOUT_SECONDS = 600` — total waktu tunggu sampai batch `succeeded`
  (async, bisa berisi ratusan polling `batch.get` @ 3 detik interval).

**Sudah dibahas di §1.3: yang KEDUA ini sudah terbukti tidak cukup bahkan di skala 87-150 item.**
Ini bukan "kegagalan" Zep — ini kegagalan KONFIGURASI di sisi MiroFish yang harus diperbaiki
sebelum Tahap 3 dipakai produksi di skala berapapun di atas ~90 item per batch.

---

## Rekomendasi ringkas (bukan desain — murni arah, untuk fase desain berikutnya)

1. **Naikkan/parameterisasi `ZEP_INGESTION_WAIT_TIMEOUT_SECONDS` berdasarkan `item_count`**
   (model §1.2 + margin, mis. 2×) — ini BUKAN opsional, tanpa ini ingest akan gagal timeout di
   skala yang jauh lebih kecil dari yang diminta brief.
2. **Tambahkan MINIMAL satu retry di orkestrasi Tahap 3 di atas `get_news_data`** — leverage
   sangat tinggi (§2.2: 80% kegagalan sembuh dengan 1 retry sederhana), jauh lebih murah
   daripada menaikkan timeout `requests.get` itu sendiri.
3. **Reuse 1 graph_id lintas as_of_date untuk backtest** (§4.4) — DENGAN syarat: ontology fix
   dari awal (§3), dan `ZepEntityReader`/`zep_paging` di-extend untuk meneruskan `SearchFilters`
   waktu (§4.2) sebelum dipakai untuk baca balik point-in-time.
4. **Klarifikasi limit ontology tier Flex** (10 gabungan vs 10/kategori, §3) sebelum menetapkan
   ontology produksi final — murah untuk dicek (tanya Zep, atau uji coba kecil), mahal kalau
   salah asumsi di tengah jalan produksi.
5. **Putuskan kontrak Tahap 3→4** (Opsi A vs B, §5) secara eksplisit di fase desain — jangan
   biarkan implisit, karena tidak ada kode existing yang bisa jadi default netral.

## Keterbatasan/risiko yang BELUM terjawab investigasi ini

1. **Jalur multi-`batch.add()` (>350 item dalam 1 batch_id) tidak diuji live** — hanya
   diverifikasi lewat pembacaan kode (§1.1). Perilaku Zep saat menerima beberapa `batch.add()`
   berurutan pada skala besar (mis. urutan sampai/tidaknya semua item, retry parsial di
   tengah rangkaian call) belum punya bukti langsung.
2. **Model linear §1.2 hanya dari 2 titik data** (87 & 150 item) — belum divalidasi di
   skala 300-900 item sungguhan (dicoba di sesi ini, tapi tingkat kegagalan fetch Finnhub
   §2.2 membuat pengumpulan 270+ item nyata dalam waktu sesi yang wajar tidak tercapai; yang
   terkumpul 150). Kalau Zep punya perilaku non-linear di atas ini (throttling internal,
   antrian project-wide), model akan meleset.
3. **Ambiguitas limit ontology tier Flex** (§3) — 10 gabungan atau 10/kategori, tidak
   dikonfirmasi dari dokumentasi manapun yang diakses, dan tidak diuji live (ontology test
   terlalu kecil untuk mendekati limit).
4. **Filter `valid_at`/`invalid_at` untuk baca balik point-in-time (§4.2) belum pernah diuji
   coba secara live** di investigasi manapun (ini maupun sebelumnya) — keberadaannya dikonfirmasi
   dari tipe SDK (`SearchFilters`) dan dari perilaku temporal invalidation yang sudah divalidasi
   `validate_zep_cloud_integration.py`, TAPI belum ada bukti langsung bahwa mem-filter dengan
   tanggal spesifik menghasilkan state graph yang benar untuk 1 as_of_date dari graph
   multi-hari sungguhan.
5. **Opsi A vs B kontrak Tahap 3→4 (§5) benar-benar terbuka** — investigasi ini sengaja tidak
   memutuskan, sesuai scope ("investigasi, bukan desain").

---

## Lampiran — parameter run live investigasi ini (untuk reproduksi)

```
Run baru (2026-09-22, sesi ini):
  universe          = screen_universe(sectors=None, market_cap_tiers=None) -> 503 kandidat
                       (503/503 S&P 500 lolos filter kosong, terurut market cap menurun)
  tickers dicoba    = 160 pertama dari daftar (didominasi mega/large-cap krn urutan market cap)
  news fetch        = get_news_data(ticker, as_of_date=None), live Finnhub, tanpa retry
                       -> 50/160 sukses, 110/160 gagal (semua "read timeout=15", 0 dengan
                          artikel kosong)
  diagnosis lanjutan = re-panggil get_news_data PERSIS pada 110 ticker gagal tsb
                       -> 88/110 (80%) sukses di percobaan kedua (200-250 artikel/ticker),
                          22/110 (20%) gagal lagi (timeout persisten)
  items ke Zep      = 150 item, 3 artikel/ticker (top recency, dedup article_id) dari 50 ticker
                       yang sukses di percobaan pertama
  ontology          = SAMA seperti investigasi sebelumnya: Company(ticker)/Person/
                       MarketEvent(event_type); edges INVOLVES(MarketEvent->Company),
                       COMMENTS_ON(Person->Company)
  Zep graph_id (dihapus setelah investigasi) = mirofish_scaletest_1790085678
  Zep batch_id      = 84ae933a-d56c-48c9-b790-d85b42965f9e
  hasil batch       = status=succeeded, submit=1,7s, wait=705,9s, 150/150 episode
  entity terbaca    = 320 node total, 202 terfilter (Company/MarketEvent/Person), 6,0s baca balik
  total waktu skrip = 2.328 detik (~38,8 menit; termasuk screening 503 ticker 286,8s +
                       fetch 1.326,1s + ingest+baca-balik ~712s)
```

Skrip investigasi (sekali-pakai, tidak di-commit, tersimpan di scratchpad sesi) memanggil
`screen_universe`, `get_news_data`, `GraphBuilderService`, `ZepEntityReader` PERSIS seperti
produksi, tanpa modifikasi. Graph uji dihapus di blok `finally` setelah pembacaan selesai.
