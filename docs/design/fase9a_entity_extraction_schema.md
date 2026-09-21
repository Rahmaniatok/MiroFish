# Fase 9a — Desain Ekstraksi Entity/Edge Antar-Perusahaan dari Berita

> **SUPERSEDED** — digantikan oleh fase9a_metric_based_edges.md.
> Didokumentasikan tetap untuk jejak keputusan (cross-mention rate 5.4% <
> ambang 10%), BUKAN dihapus.

Status: **desain, belum implementasi** (Fase 9b menunggu review dokumen ini —
JANGAN mulai implementasi setelah dokumen ini selesai).

Konteks: Fase 8 (8a sumber, 8b skema, 8c implementasi —
[docs/design/fase8b_news_schema.md](fase8b_news_schema.md)) sudah selesai.
`get_news_data(ticker, as_of_date)` (`backend/app/data_layer/news_data.py`)
mengembalikan artikel per `(ticker, as_of_date)` tanpa cap jumlah, dengan
`article_id` sebagai dedup key yang **belum dipakai di mana pun** — dedup jadi
tanggung jawab konsumen. Dokumen ini adalah konsumen pertama itu.

Entity graph existing (Fase 2, `financial_entity_extractor.py` +
`entity_edge_builder.py`) murni deterministik tanpa LLM: bintang berpusat di
SATU entitas Company per ticker, menunjuk ke atribut-atribut dirinya sendiri
(Sector, ValuationMetric, FundamentalMetric, TechnicalSignal, ShariaScreen).
Tidak ada edge Company-to-Company. Fase 9 adalah titik PERTAMA LLM masuk ke
entity graph, dengan tujuan membangun edge ANTAR-PERUSAHAAN dari konten
berita — bahan seed untuk simulasi OASIS di Fase 11b nanti.

**Scope dokumen ini murni desain.** Tidak ada file baru/berubah di
`backend/app/services/` atau `backend/app/data_layer/`. Referensi yang dibaca
(tidak diubah): `financial_entity_extractor.py`, `entity_edge_builder.py`,
`seed_builder.py`, `news_data.py`, `zep_entity_reader.py`, dan pola
`_derive_investor_stance`/`_investor_persona_via_llm` di
`oasis_profile_generator.py` (Fase 3a/5a) untuk pola "angka pasti dulu, LLM
menulis narasi di sekitarnya".

---

## Batasan sumber data yang HARUS diakui di sini (bukan diabaikan)

Fase 8b **tidak menyimpan body artikel penuh** — hanya `headline` (wajib) dan
`summary` (nullable, Finnhub kadang mengosongkannya). Ini bukan detail kecil:
**seluruh permukaan teks yang LLM Fase 9 boleh baca dan kutip sebagai evidence
adalah headline + summary saja**, bukan artikel utuh. Konsekuensinya menembus
setiap keputusan di bawah:
- Ketika `summary` ada, LLM punya beberapa kalimat untuk bekerja.
- Ketika `summary` kosong (`None`), LLM hanya punya SATU baris headline —
  ekstraksi tetap dicoba (headline sering sudah memuat relasi eksplisit,
  mis. "Apple and Microsoft Team Up on AI Chips"), tapi hasil untuk artikel
  begini ditandai eksplisit `text_used: "headline_only"` di attributes edge
  (bagian 3.4), supaya konsumen downstream tahu dasar buktinya tipis — sama
  filosofinya dengan `is_point_in_time` di Fase 1f/1g: tandai keterbatasan,
  jangan sembunyikan.
- Ini juga berarti graph Company-to-Company hasil Fase 9 realistis akan
  condong ke relasi yang cukup jelas untuk muncul di headline/ringkasan
  singkat (co-mentioned, partnership/deal yang diumumkan) — bukan nuansa
  relasi yang butuh baca artikel penuh. **Trigger untuk revisit**: kalau hasil
  Fase 9b ternyata terlalu jarang/dangkal secara operasional, opsi follow-up
  adalah merevisi Fase 8b untuk menyimpan lebih banyak teks — di luar scope
  sekarang, dicatat sebagai keterbatasan sadar bukan diperbaiki diam-diam.

---

## 1. Dedup di Titik Masuk — Algoritma Konkret

### 0. Validasi Empiris Sebelum Implementasi (WAJIB dijalankan sebagai bagian pertama Fase 9b, sebelum kode ekstraksi LLM ditulis)

Bagian "Batasan sumber data" di atas mengakui risiko sparsity headline+summary,
tapi murni deferred ("trigger revisit KALAU NANTI hasilnya jarang") tanpa
diukur. Sebelum menulis kode ekstraksi LLM, jalankan pengecekan MURAH (tanpa
LLM, cukup keyword/regex sederhana — cek apakah nama/ticker perusahaan lain
yang ada di universe run muncul sebagai substring di headline+summary
artikel): ambil sample artikel yang sudah ter-cache dari Fase 8 (minimal 50
artikel dari beberapa ticker berbeda), hitung persentase yang menyebut >=2
entitas perusahaan berbeda dalam headline+summary gabungan. Laporkan angka
ini sebelum lanjut ke implementasi LLM penuh.

Kalau persentase sangat rendah (<10%, ambang ilustratif), ini konfirmasi
empiris bahwa graph Company-to-Company akan sangat sparse — pada titik itu,
**putuskan bersama (bukan lanjut diam-diam)** apakah tetap lanjut dengan
ekspektasi sparse yang terdokumentasi, atau eskalasi ke revisi Fase 8b
(simpan body artikel). Ini pengecekan murah (menit, bukan jam) yang mencegah
investasi penuh ke pipeline LLM berbiaya mahal sebelum tahu apakah datanya
cukup kaya untuk tugas ini.

**Titik pengelompokan**: SEBELUM pemanggilan LLM manapun, di sebuah langkah
orkestrasi baru (bagian dari Fase 9b, di luar `data_layer`/`services` yang
sudah ada) yang berjalan SEKALI per `as_of_date` yang sedang diproses dalam
satu run — bukan per ticker, dan bukan di dalam `news_data.py` (yang secara
eksplisit menyatakan "TIDAK ADA logika dedup lintas as_of_date di sini — itu
ranah Fase 9" di komentar modulnya).

Kenapa lintas-ticker (bukan hanya lintas-`as_of_date` untuk satu ticker):
Finnhub `company-news` di-query PER TICKER (`symbol=X`). Satu artikel yang
sama persis (`id` sama) bisa muncul di hasil query untuk AAPL ("Apple dan
Microsoft..." disebut relevan untuk AAPL) DAN muncul lagi di hasil query
untuk MSFT (artikel yang SAMA, `related_ticker` beda karena itu murni echo
ticker yang di-query — sudah dikonfirmasi Fase 8b bagian 1). Tanpa dedup
lintas-ticker, LLM akan diminta mengekstrak relasi dari artikel yang SAMA dua
kali secara independen — boros biaya DAN berisiko menghasilkan dua edge yang
sedikit berbeda framing-nya untuk fakta yang identik.

**Algoritma (pseudocode ilustratif, urutan proses eksplisit):**

```python
# ILUSTRASI — bukan implementasi. Berjalan sekali per as_of_date, atas
# daftar tickers TETAP untuk run itu (lihat catatan konsistensi as_of_date
# di bagian "Catatan tambahan" — ini memakai constituents snapshot yang sama
# yang sudah disyaratkan Fase 8a/8c untuk seluruh loop backtest).

@dataclass
class DedupedArticle:
    article_id: int
    headline: str
    summary: Optional[str]
    publisher: Optional[str]
    url: str
    published_at: str
    related_tickers: List[str]   # UNION dari semua ticker yang membawa artikel ini


def dedup_articles_for_run(tickers: List[str], as_of_date: str) -> List[DedupedArticle]:
    # Langkah 1 — FETCH DULU untuk semua ticker (Fase 8c, sudah ber-cache;
    # tidak ada panggilan Finnhub baru yang dipicu di sini, murni baca cache
    # atau cache-miss existing dari get_news_data).
    per_ticker_results = {
        ticker: get_news_data(ticker, as_of_date=as_of_date)   # Fase 8c
        for ticker in tickers
    }

    # Langkah 2 — GROUP BY article_id, UNION related_ticker. Titik
    # pengelompokan literal: satu dict keyed by article_id, diisi berurutan
    # per ticker (urutan tickers tidak memengaruhi hasil akhir — union
    # bersifat komutatif).
    by_id: Dict[int, DedupedArticle] = {}
    for ticker, result in per_ticker_results.items():
        if not result.get("success") or not result.get("articles"):
            continue  # articles=[] valid (Fase 8b) — cukup tidak menyumbang apa pun
        for raw in result["articles"]:
            article_id = raw["article_id"]
            if article_id not in by_id:
                by_id[article_id] = DedupedArticle(
                    article_id=article_id,
                    headline=raw["headline"],
                    summary=raw["summary"],
                    publisher=raw["publisher"],
                    url=raw["url"],
                    published_at=raw["published_at"],
                    related_tickers=[ticker],
                )
            else:
                existing = by_id[article_id]
                # UNION — inilah baris yang menjawab "digabung bagaimana":
                # ticker baru ditambahkan kalau belum ada, urutan tickers TETAP
                # dijaga stabil (insertion order) supaya hasil deterministik
                # untuk keperluan test/replay, bukan di-sort ulang tiap saat.
                if ticker not in existing.related_tickers:
                    existing.related_tickers.append(ticker)
                # Defensive check — field konten SEHARUSNYA identik untuk
                # article_id yang sama (Finnhub echo, bukan data per-ticker
                # yang beda). Kalau ternyata beda, itu ANOMALI sumber data:
                # log peringatan, JANGAN diam-diam menimpa (konsisten dengan
                # gaya "flag, don't fix silently" di codebase ini).
                if existing.headline != raw["headline"]:
                    logger.warning(
                        "article_id=%s headline berbeda antar ticker query "
                        "(%r vs %r) — mempertahankan versi pertama-terlihat",
                        article_id, existing.headline, raw["headline"],
                    )

    # Langkah 3 — HASIL: satu list artikel UNIK, tiap entri membawa daftar
    # SEMUA ticker yang relevan dengannya. Baru SEKARANG dikirim ke LLM
    # (langkah berikutnya, bagian 3) — DEDUP DULU, LLM BELAKANGAN, tidak
    # pernah sebaliknya.
    return list(by_id.values())
```

**Urutan proses ditegaskan ulang secara eksplisit** (karena ini poin yang
"sudah disepakati sebelumnya" dan wajib tidak dilanggar oleh implementasi
Fase 9b nanti):

1. Fetch (Fase 8c, per ticker, cached) — SEMUA ticker dalam run untuk satu `as_of_date`.
2. Dedup + union `related_tickers` by `article_id` — SEKALI, lintas seluruh ticker.
3. **Baru sesudah itu** setiap artikel unik dikirim ke LLM (bagian 3) — TEPAT SATU KALI per artikel unik, terlepas dari berapa banyak ticker yang membawanya.

Kalau urutan dibalik (LLM dipanggil per-ticker sebelum dedup), artikel yang
sama akan diproses LLM berkali-kali dengan `related_tickers` yang salah
(hanya melihat SATU ticker per panggilan, bukan union yang sebenarnya) —
persis kesalahan yang harus dihindari.

---

## 2. Skema Entity & Edge Baru

### 2.1 Posisi: EXTEND `FilteredEntities` existing, bukan struktur terpisah

**Keputusan: perluas `FilteredEntities`/`EntityNode` yang sudah ada — entity
baru (`Event`) jadi tipe baru di `labels[0]` yang sama, edge Company-to-Company
memakai BENTUK edge dict yang identik dengan konvensi Fase 2c
(`{direction, edge_name, fact, target_node_uuid | source_node_uuid}`).**

Alasan:
- `FilteredEntities` sudah jadi kontrak yang dikonsumsi banyak tempat
  (`oasis_profile_generator`, `simulation_config_generator`, adapter graph
  frontend `graphAdapter.js`). Struktur terpisah berarti SETIAP konsumen itu
  butuh cabang kode baru untuk "kalau ada Fase-9-graph, gabungkan dulu" —
  memindahkan kerumitan ke banyak tempat alih-alih satu tempat.
- Bentuk edge Fase 2c (outgoing di sumber, incoming di target, `fact` string
  sama persis di kedua ujung) sudah generik — tidak ada asumsi bahwa target
  edge harus berupa "atribut", jadi dipakai ulang APA ADANYA untuk edge
  Company-to-Company. Tidak perlu bentuk edge baru.
- Konsekuensi struktural penting (dijelaskan penuh di bagian 4): karena Fase 2
  membangun SATU bintang per ticker (`build_seed_from_ticker(ticker)`), edge
  Company-to-Company Fase 9 butuh graph GABUNGAN multi-ticker terlebih dulu
  sebelum bisa menaut dua Company yang berasal dari dua pemanggilan
  `build_seed_from_ticker` yang terpisah. Ini BUKAN alasan untuk struktur
  terpisah — cukup satu langkah merge (ilustratif, bagian 4) sebelum Fase 9
  menambahkan edge-nya.

### 2.2 Entity baru: `Event`

**Keputusan: perlu tipe baru `ENTITY_TYPE_EVENT = "Event"`** — TIDAK bisa
dipetakan ke 6 tipe existing, karena ke-6-nya (Company/Sector/ValuationMetric/
FundamentalMetric/TechnicalSignal/ShariaScreen) semuanya berbentuk "fakta
identitas atau metrik numerik pada satu titik waktu", sedangkan Event
berbentuk "kejadian naratif" (earnings call, product launch, partnership
announcement, regulatory action) — tidak punya `value`/`unit` numerik, punya
`event_type` + `event_date` + deskripsi + artikel sumber sebagai gantinya.
Memaksakannya ke `FundamentalMetric` atau `TechnicalSignal` akan merusak
asumsi existing bahwa entitas di tipe itu SELALU punya `value` numerik
(dipakai langsung oleh `_derive_investor_stance` dkk. di Fase 3/5a).

Event **opsional per artikel** — LLM hanya membuat Event kalau artikel
benar-benar melaporkan satu kejadian konkret bertanggal (bukan tiap artikel
otomatis jadi Event; artikel murni "co-mentioned dalam analisis pasar" tidak
perlu Event).

```python
# ILUSTRASI — bentuk EntityNode.to_dict() untuk Event, TIDAK ubah
# financial_entity_extractor.py di sini (Fase 9b yang akan menambahkannya).
ENTITY_TYPE_EVENT = "Event"   # ditambahkan ke FINANCIAL_ENTITY_TYPES nanti

{
    "uuid": "evt::12345::partnership",     # f"evt::{article_id}::{slug(event_type)}"
    "name": "Apple–Microsoft AI Chip Partnership",
    "labels": ["Event", "Entity"],
    "summary": "Apple and Microsoft announced a joint venture to co-design "
                "custom AI datacenter chips, reported 2026-08-20.",
    "attributes": {
        "event_type": "partnership",       # lihat enum bagian 2.4
        "event_date": "2026-08-20",         # dari published_at artikel, BUKAN as_of_date run
        "source_article_ids": [12345],
        "involved_tickers": ["AAPL", "MSFT"],
        "confidence": 0.93,
        "text_used": "summary",             # "headline_only" | "summary" — lihat catatan pembuka
    },
    "related_edges": [],   # diisi lewat edge involves_company, lihat 2.3
    "related_nodes": [],
}
```

**Catatan kompatibilitas frontend**: sebelum Fase 9b menambahkan tipe `Event`,
cek `graphAdapter.js` (konsumen `FilteredEntities` di frontend, Fase 7b) —
pastikan ada fallback graceful untuk label node yang tidak dikenali
sebelumnya, supaya node berlabel `["Event", "Entity"]` tidak menyebabkan
render gagal/crash. Kalau belum ada fallback, ini perlu ditambahkan SEBELUM
`Event` pertama muncul di data, bukan ditemukan saat testing frontend.

### 2.3 Edge Company-to-Company: field wajib

Field wajib per edge (di `attributes` edge, KECUALI `direction`/`edge_name`/
`fact`/`target_node_uuid`/`source_node_uuid` yang tetap di level top seperti
konvensi Fase 2c):

| Field | Wajib/Opsional | Alasan |
|---|---|---|
| `relation_type` | **Wajib** | Jenis relasi — lihat enum 2.4. Menentukan `edge_name` di level top. |
| `symmetric` | **Wajib** (bool) | Apakah relasi ini dua-arah secara semantik (competitor, co-mentioned) atau punya arah nyata (supplier→customer). Konsumen traversal graph HARUS tahu ini supaya tidak salah baca "A supplies_to B" sebagai fakta yang juga berlaku terbalik. |
| `confidence` | **Wajib** (float 0..1) | Skor keyakinan LLM — dipakai Fase 11b nanti sebagai bobot edge dalam simulasi (bukan untuk memfilter di Fase 9 sendiri, lihat bagian 3.3). |
| `evidence` | **Wajib**, minimal 1 item, TIDAK BOLEH kosong | Traceability + hallucination guard (bagian 3.3) — daftar `{article_id, quote, published_at}`. Edge TANPA evidence yang lolos verifikasi DITOLAK, tidak pernah disimpan dengan evidence kosong. |
| `source_published_at` | **Wajib** | Tanggal publish artikel PALING AWAL di antara semua evidence yang menyumbang edge ini (first-reported date) — diupdate ulang setiap kali merge menambah evidence baru yang lebih awal dari nilai saat ini, tidak pernah mundur. Dihitung ulang sebagai `min(evidence[].published_at)` setiap kali merge terjadi — tidak perlu state tambahan di luar `evidence` list itu sendiri. |
| `latest_evidence_published_at` | **Wajib** | Tanggal publish artikel PALING BARU di antara evidence (last-confirmed date) — diupdate naik setiap merge, dihitung sebagai `max(evidence[].published_at)`. Sama dengan `source_published_at` untuk edge dari 1 artikel (min=max). |
| `as_of_date` | **Wajib** | `as_of_date` run tempat edge ini dibangun — sama makna dengan field `as_of_date` di entitas Fase 2 (Company/metrics), supaya konsisten dilihat "berlaku pada snapshot backtest kapan". |
| `text_used` | **Wajib** (`"headline_only"` \| `"headline+summary"`) | Transparansi keterbatasan sumber (lihat catatan pembuka). |
| `merged_from_article_count` | **Wajib**, default 1 | Berapa artikel berbeda yang menyumbang edge ini setelah merge (bagian 2.5) — 1 kalau belum pernah digabung. |
| `direction_note` | Opsional | Prosa singkat kalau `symmetric=False`, menjelaskan arah literal (mis. "AAPL adalah pemasok, MSFT adalah pembeli") — hanya diisi kalau arah relevan dan LLM cukup yakin, tidak dipaksakan. |

```python
# ILUSTRASI — pasangan edge Company-to-Company, mengikuti KONVENSI FASE 2c
# PERSIS (outgoing di sumber, incoming di target, fact sama persis).

# di AAPL::company.related_edges
{
    "direction": "outgoing",
    "edge_name": "partner_of",
    "fact": "Apple Inc. (AAPL) has a reported partner_of relationship with "
            "Microsoft Corp. (MSFT), evidenced by 1 article (2026-08-20).",
    "target_node_uuid": "MSFT::company",
    "attributes": {
        "relation_type": "partner_of",
        "symmetric": True,   # DERIVED dari _NEWS_EDGE_SPEC (bagian 3.3), bukan dari respons LLM
        "confidence": 0.93,
        "evidence": [
            {
                "article_id": 12345,
                "published_at": "2026-08-20T14:30:00Z",
                "quote": "Apple and Microsoft today announced a joint venture "
                         "to co-design custom AI datacenter chips.",
            }
        ],
        "source_published_at": "2026-08-20T14:30:00Z",         # min(evidence[].published_at)
        "latest_evidence_published_at": "2026-08-20T14:30:00Z",  # max(evidence[].published_at) — sama dgn di atas (1 artikel, min=max)
        "as_of_date": "2026-08-20",
        "text_used": "summary",
        "merged_from_article_count": 1,
    },
}

# di MSFT::company.related_edges (fact SAMA PERSIS, arah dibalik)
{
    "direction": "incoming",
    "edge_name": "partner_of",
    "fact": "Apple Inc. (AAPL) has a reported partner_of relationship with "
            "Microsoft Corp. (MSFT), evidenced by 1 article (2026-08-20).",
    "source_node_uuid": "AAPL::company",
    "attributes": { ... sama persis dict di atas ... },
}
```

### 2.4 Enum `relation_type` (edge_name) — closed set, ilustratif

Mengikuti gaya `_EDGE_SPEC` di `entity_edge_builder.py` (dict eksplisit, bukan
string bebas dari LLM):

```python
# ILUSTRASI — mirror gaya _EDGE_SPEC existing
_NEWS_EDGE_SPEC: Dict[str, Dict[str, Any]] = {
    "competitor_of":    {"symmetric": True,  "noun": "competitor"},
    "supplies_to":       {"symmetric": False, "noun": "supplier relationship"},
    "partner_of":        {"symmetric": True,  "noun": "partner"},
    "owns_stake_in":     {"symmetric": False, "noun": "ownership stake"},
    "in_dispute_with":   {"symmetric": True,  "noun": "dispute"},
    "co_mentioned_with": {"symmetric": True,  "noun": "co-mention"},  # fallback teraman
}
```

LLM **wajib memilih dari enum ini** (bagian 3.2 — bukan bikin string bebas).
`co_mentioned_with` sengaja jadi **fallback teraman**: dipakai kalau LLM bisa
lihat dua perusahaan disebut bersama dan berkaitan, tapi TIDAK cukup yakin
untuk mengklaim jenis relasi yang lebih spesifik. Prinsip: lebih baik
under-claim (`co_mentioned_with`) daripada over-claim (`competitor_of`) kalau
bukti tipis — konsisten dengan filosofi hallucination guard di bagian 3.3.

### 2.5 Menangani edge yang sama muncul dari BANYAK artikel — MERGE, bukan duplikat

**Keputusan: MERGE jadi satu edge per `(pasangan company, relation_type)`,
dengan evidence GABUNGAN dan confidence GABUNGAN — bukan tetap N edge
terpisah.**

Alasan tetap-satu-edge: konsumen graph (Fase 11b OASIS seeding, atau
traversal manapun) akan query "apa hubungan AAPL-MSFT" dan mengharapkan SATU
jawaban dengan bukti terkumpul, bukan harus men-dedupe N edge duplikat
sendiri di setiap titik konsumsi — sama alasannya dengan kenapa Fase 2c
membangun edge SEKALI per (Company, target) alih-alih membiarkan konsumen
menghitung ulang.

**Kunci merge — WAJIB bergantung pada simetri relasi (`symmetric`, bagian
2.4), tidak boleh satu formula untuk semua `relation_type`**:

- Untuk `relation_type` dengan `symmetric=True` (dari `_NEWS_EDGE_SPEC`,
  bagian 2.4): kunci merge = `(min(ticker_a, ticker_b), max(ticker_a,
  ticker_b), relation_type)` — arah tidak relevan secara semantik, sorted
  tuple aman dan urutan proses artikel tidak memengaruhi hasil.
- Untuk `relation_type` dengan `symmetric=False`: kunci merge WAJIB
  menyertakan ARAH ASLI, **TIDAK di-sort**: `(ticker_a, ticker_b,
  relation_type)` dengan `ticker_a`=source, `ticker_b`=target APA ADANYA dari
  klaim LLM. Menyortir di sini akan SALAH — dua klaim dengan arah berlawanan
  untuk pasangan yang sama (mis. "AAPL supplies_to MSFT" vs "MSFT
  supplies_to AAPL") punya arti yang KONTRADIKTIF, bukan variasi framing dari
  fakta yang sama, jadi tidak boleh otomatis tergabung jadi satu edge dengan
  confidence gabungan.

Di kedua kasus, `relation_type` tetap ikut di kunci karena DUA relasi
berbeda pada pasangan yang sama (mis. AAPL-MSFT sekaligus `competitor_of`
DAN `partner_of` — masuk akal: bersaing di satu lini, berpartner di lini
lain) disimpan sebagai edge TERPISAH, bukan dipaksa jadi satu.

**Deteksi kontradiksi arah (WAJIB ditangani, bukan diabaikan)**: kalau
`relation_type` sama, pasangan ticker sama, TAPI arah berlawanan muncul dari
artikel berbeda (mis. artikel A bilang "AAPL supplies_to MSFT", artikel B
bilang "MSFT supplies_to AAPL") — kedua kunci merge di atas (tidak di-sort)
akan menghasilkan DUA entri terpisah, bukan satu. Ini SENGAJA — **jangan
digabung jadi satu edge, dan jangan diam-diam pilih salah satu arah sebagai
"yang benar"**. Kedua arah disimpan sebagai edge terpisah, masing-masing
dengan field tambahan:

| Field | Wajib/Opsional | Arti |
|---|---|---|
| `conflicting_direction` | **Wajib** (bool), default `False` | `True` kalau edge ini punya pasangan arah-berlawanan untuk `(pasangan ticker, relation_type)` yang sama dalam graph yang sama. |
| `conflicting_edge_ref` | **Wajib kalau `conflicting_direction=True`** | uuid pasangan Company di ujung lain edge yang berlawanan arah, supaya konsumen bisa menelusuri kedua klaim + evidence masing-masing. |

Fase 9 tugasnya cuma MELAPORKAN kontradiksi ini secara eksplisit lewat kedua
field di atas — bukan menyembunyikannya lewat merge, dan bukan memutuskan
mana yang benar. Konsumen downstream (Fase 11b) yang memutuskan cara
menangani konflik ini (mis. menurunkan confidence keduanya, atau
menampilkannya sebagai ketidakpastian eksplisit dalam simulasi) — keputusan
itu di luar scope Fase 9.

**Kombinasi evidence**: list evidence dari semua artikel yang mendukung edge
yang sama digabung (concat, TIDAK dibatasi jumlahnya — konsisten dengan
filosofi "tanpa cap" Fase 8b), `merged_from_article_count` naik sesuai jumlah
artikel unik penyumbang.

**Kombinasi confidence — formula eksplisit, bukan rata-rata / bukan max
begitu saja**:

```
confidence_merged = 1 - Π(1 - c_i)   untuk setiap artikel i yang menyumbang,
                                       dibatasi maksimum 0.98
```

(probabilistic-OR: tiap artikel dianggap bukti independen untuk fakta yang
sama; makin banyak artikel mengonfirmasi, makin tinggi confidence gabungan,
naik monoton — TIDAK seperti rata-rata yang bisa turun kalau satu artikel
confidence-nya rendah padahal artikel lain sudah sangat yakin). Dibatasi
0.98 (bukan 1.0) supaya tidak pernah mengklaim kepastian mutlak murni dari
akumulasi artikel — tetap ada headroom ketidakpastian, konsisten dengan
seluruh graph ini bersumber dari LLM (bukan data terverifikasi seperti harga
saham).

Contoh: AAPL-MSFT `partner_of` disebut di 3 artikel dengan confidence
individual 0.93, 0.80, 0.60 →
`1 - (1-0.93)(1-0.80)(1-0.60) = 1 - 0.07×0.20×0.40 = 1 - 0.0056 = 0.9944`
→ dibatasi ke **0.98**.

---

## 3. Desain Prompt LLM & Structured Output

### 3.1 Granularitas panggilan: SATU ARTIKEL = SATU PANGGILAN LLM

**Keputusan sadar (bukan default tak-dipikir), dengan alternatif yang
ditimbang eksplisit:**

| Opsi | Kelebihan | Kekurangan | Keputusan |
|---|---|---|---|
| **A. 1 artikel = 1 panggilan LLM** (dipilih) | Evidence attribution trivial — setiap quote otomatis milik SATU `article_id` yang sedang diproses, tidak ada risiko LLM salah atribusi quote ke artikel yang salah. Prompt pendek & respons kecil -> risiko JSON terpotong (pola `finish_reason == "length"` yang sudah ditangani di `oasis_profile_generator`) jauh lebih kecil. Embarrassingly parallel — bisa di-async/batch banyak panggilan sekaligus tanpa saling bergantung, karena tidak ada state bersama antar artikel. | Jumlah panggilan LLM = jumlah artikel unik per `as_of_date` run (bisa ratusan untuk universe besar, lihat Fase 8b skenario d) — biaya lebih tinggi dibanding batching. | ✅ Biaya LLM sudah disepakati boleh mahal (instruksi task); manfaat verifiability jauh lebih berharga untuk graph yang akan dipakai sebagai dasar simulasi OASIS. |
| **B. Batch N artikel per panggilan** | Lebih sedikit panggilan -> lebih murah. | LLM harus menjaga N artikel tetap terpisah dalam satu respons -> risiko nyata quote/evidence dari artikel X ke-atribusi salah ke artikel Y (persis pola halusinasi yang harus dicegah, bagian 3.3). Respons JSON jauh lebih besar -> risiko truncation naik. | ❌ Ditolak untuk fase ini — trade-off verifiability tidak sepadan dengan penghematan biaya yang secara eksplisit tidak jadi kendala. |

### 3.2 Structured output — JSON schema ilustratif

Mengikuti pola `response_format={"type": "json_object"}` +
`create_chat_completion` + retry-dengan-temperature-turun yang SUDAH ADA di
`oasis_profile_generator._investor_persona_via_llm` (bukan pola baru).

```python
# ILUSTRASI — bukan file .py yang dijalankan.

def _news_extraction_system_prompt() -> str:
    return (
        "You extract COMPANY-TO-COMPANY relationships from a single news "
        "article snippet (headline + optional summary — you do NOT have the "
        "full article body). You must NEVER invent a relationship that isn't "
        "directly supported by the text you were given. Every relation you "
        "report MUST include a verbatim quote from the provided text as "
        "evidence — if you cannot quote supporting text, do not report the "
        "relation. Only mention tickers from the ALLOWED TICKERS list; never "
        "invent or guess a ticker. Return valid JSON only, no unescaped "
        "newlines inside string values."
    )


def _build_news_extraction_prompt(article: DedupedArticle, allowed_tickers: List[str]) -> str:
    text_used = "headline+summary" if article.summary else "headline_only"
    body = article.headline + (f"\n\n{article.summary}" if article.summary else "")
    return f"""Article (published {article.published_at}, id={article.article_id}):
\"\"\"
{body}
\"\"\"

ALLOWED TICKERS for this run (only report relations between these; ignore
any other company mentioned): {", ".join(allowed_tickers)}

TEXT AVAILABLE: {text_used} (this article's summary was {'available' if article.summary else 'NOT available — work from the headline alone'}).

Extract:
1. "relations": company-to-company relationships between two ALLOWED TICKERS
   that this text directly supports. For each relation, pick relation_type
   from EXACTLY this set: competitor_of, supplies_to, partner_of,
   owns_stake_in, in_dispute_with, co_mentioned_with. If you are not
   confident which specific type applies but the two companies are clearly
   related in this text, use "co_mentioned_with" rather than guessing a
   stronger claim.
2. "event" (optional, omit if this article does not report one concrete
   dated occurrence): a single Event with event_type (one of: earnings_call,
   product_launch, partnership_announcement, regulatory_action, litigation,
   other) and a one-sentence description.

Return a JSON object with exactly this shape:
{{
  "relations": [
    {{
      "ticker_a": "<from ALLOWED TICKERS>",
      "ticker_b": "<from ALLOWED TICKERS, != ticker_a>",
      "relation_type": "<one of the 6 types above>",
      "confidence": <float 0..1>,
      "evidence_quote": "<verbatim substring copied from the article text above>"
    }}
  ],
  "event": {{
    "event_type": "<one of the 6 types above>",
    "description": "<one sentence>",
    "involved_tickers": ["<from ALLOWED TICKERS>", ...],
    "evidence_quote": "<verbatim substring copied from the article text above>"
  }}
}}

If there are no qualifying relations, return {{"relations": [], "event": null}}.
"""
```

Field wajib/opsional di respons:
- `relations`: **wajib** (boleh array kosong).
- Tiap relation: `ticker_a`/`ticker_b`/`relation_type`/`confidence`/
  `evidence_quote` — **semua wajib**, tidak ada yang opsional di level
  relation (relation TANPA evidence_quote ditolak saat parsing, bukan
  diterima dengan `evidence_quote: null`). **`symmetric` SENGAJA TIDAK
  diminta dari LLM** — lihat bagian 3.3 langkah 4a: field ini di-derive
  langsung dari `_NEWS_EDGE_SPEC[relation_type]`, karena sudah merupakan
  properti TETAP per `relation_type` (bagian 2.4). Meminta LLM mengisinya
  ulang akan membuka dua sumber kebenaran yang bisa saling bertentangan
  kalau LLM salah isi, untuk sesuatu yang seharusnya deterministik.
- `event`: **opsional**, `null` valid, HANYA diisi kalau artikel benar-benar
  melaporkan satu kejadian bertanggal.

### 3.3 Mencegah halusinasi — pola existing diterapkan ulang

Pola yang sudah ADA di codebase (Fase 3a/5a): `_derive_investor_stance`
menghitung stance/score dari ANGKA PASTI (P/E, ROE, RSI, dst) SEBELUM LLM
dipanggil sama sekali — LLM (`_investor_persona_via_llm`) hanya menulis
prosa di sekitar `evidence` yang SUDAH berisi angka nyata, tidak pernah
menentukan angkanya sendiri. Prinsip intinya: **pisahkan "apa yang benar"
(dihitung deterministik) dari "bagaimana menceritakannya" (LLM).**

Untuk Fase 9, "apa yang benar" tidak bisa dihitung deterministik (relasi
antar-perusahaan bukan angka dari API) — jadi pola itu diadaptasi jadi
**verifikasi tekstual pasca-LLM**, bukan pra-komputasi:

1. **Evidence wajib, tanpa pengecualian** (bagian 2.3/3.2) — relation tanpa
   `evidence_quote` DITOLAK saat parsing respons, sebelum verifikasi apa pun.
2. **Verifikasi balik ke sumber (hard gate)**: setiap `evidence_quote`
   dicocokkan sebagai substring (dengan normalisasi whitespace/case
   ringan — BUKAN fuzzy-match longgar) terhadap teks asli yang benar-benar
   dikirim ke LLM (`headline` + `summary` artikel tersebut, field yang sama
   yang dipakai membangun prompt). **Kalau quote TIDAK ditemukan sebagai
   substring di teks sumber, edge itu DIBUANG** — bukan disimpan dengan
   confidence lebih rendah, bukan "diberi peringatan". Ini gerbang keras,
   sama filosofinya dengan leakage guard `fetch_price_data`/`fetch_news_data`
   (defense-in-depth: tidak percaya penuh output LLM untuk sesuatu yang
   traceability-nya krusial).
3. **Allow-list ticker eksplisit** (bagian 3.2 prompt): LLM hanya boleh
   menyebut ticker dari daftar yang sudah pasti ada di universe/run ini.
   Relation yang menyebut ticker DI LUAR allow-list **dibuang** (di-log
   sebagai "unknown ticker mention", TIDAK otomatis menambah node baru ke
   graph — universe run ini punya batas node tetap dari screening, Fase 9
   tidak berhak memperluasnya diam-diam).
4. **Enum tertutup untuk `relation_type`** (bagian 2.4) — respons dengan
   `relation_type` di luar 6 nilai yang diizinkan dibuang saat parsing (sama
   pola dengan `_try_fix_json`/validasi field wajib di
   `oasis_profile_generator`).
4a. **`symmetric` di-DERIVE, tidak pernah dibaca dari respons LLM** — SETELAH
   `relation_type` divalidasi ada di enum (langkah 4), `symmetric` diambil
   langsung dari `_NEWS_EDGE_SPEC[relation_type]["symmetric"]` (bagian 2.4).
   LLM tidak diminta mengisi field ini sama sekali (bagian 3.2) — ini
   properti TETAP per `relation_type`, bukan sesuatu yang perlu/boleh
   dinilai ulang per artikel, jadi tidak ada permukaan halusinasi untuk
   field ini.
5. **`confidence` TIDAK dipakai sebagai filter di Fase 9** — disimpan apa
   adanya (setelah merge probabilistic-OR di bagian 2.5) untuk dipakai Fase
   11b sebagai bobot simulasi. Keputusan sadar: mencampur "threshold
   kualitas ekstraksi" dengan "bobot pemakaian di simulasi" akan
   menyembunyikan berapa banyak edge sebenarnya lolos vs dibuang — Fase 9
   hanya punya SATU gerbang keras (evidence-quote terverifikasi), bukan
   ambang confidence tambahan yang bisa diam-diam membuang data valid.

Ringkas alur verifikasi: **respons LLM → parse (enum + field wajib) → cocokkan
tiap `evidence_quote` ke teks sumber (substring check) → derive `symmetric`
dari `_NEWS_EDGE_SPEC` → relation lolos hanya kalau SEMUA gerbang di atas
lolos → baru masuk sebagai kandidat edge (sebelum merge lintas-artikel,
bagian 2.5, yang kuncinya sendiri bergantung pada `symmetric` hasil derive
ini).**

### 3.4 Caching Ekstraksi — WAJIB untuk Fase 9b

Karena window 90-hari antar `as_of_date` yang berdekatan saling tumpang
tindih (Fase 8b bagian 6 skenario d), artikel `article_id` yang SAMA akan
muncul lagi di dedup group untuk `as_of_date` BERIKUTNYA dalam satu backtest
loop. Isi artikel immutable — hasil ekstraksi LLM untuk `article_id` tertentu
TIDAK bergantung pada `as_of_date` mana yang sedang memprosesnya.

**REQUIREMENT untuk Fase 9b (bukan opsional)**: cache hasil ekstraksi keyed
murni by `article_id` (bukan `(article_id, as_of_date)`), tidak pernah
expire. Tanpa ini, artikel yang sama bisa menghasilkan edge berbeda antar
`as_of_date` dalam satu backtest run yang sama akibat non-determinisme LLM —
melanggar tujuan reproducibility yang jadi alasan utama keputusan ini.
Ini bukan cuma soal biaya (sudah disepakati boleh mahal) — determinisme lintas
run backtest adalah alasannya, dan karena itu statusnya wajib, bukan
rekomendasi opsional: artikel yang sama harus selalu menghasilkan edge yang
sama, tidak bergantung pada urutan `as_of_date` mana yang memicu LLM pertama
kali. TTL mengikuti pola snapshot historis existing di `cache.py` (tidak
pernah expire, karena isi artikel immutable).

---

## 4. Integrasi dengan Entity Graph Existing (Fase 2)

**Keputusan: MELENGKAPI, bukan menggantikan.** Bintang struktural Fase 2
(Company → Sector/ValuationMetric/FundamentalMetric/TechnicalSignal/
ShariaScreen, dibangun `build_seed_from_ticker` + `build_entity_edges`, murni
deterministik) **tetap ada persis seperti sekarang, tidak disentuh** — sudah
dites (`test_entity_edge_builder.py`, `test_seed_builder.py`), dan sengaja
dibangun tanpa LLM secara eksplisit di komentar modulnya. Mereplace-nya tidak
perlu dan berisiko meregresi sesuatu yang sudah terverifikasi.

**Nuansa struktural yang harus dijelaskan**: `build_seed_from_ticker(ticker,
as_of_date)` Fase 2b beroperasi per-SATU-ticker — setiap pemanggilan
menghasilkan `FilteredEntities` terpisah untuk satu Company. Edge
Company-to-Company Fase 9 butuh KEDUA ujung company berada dalam SATU graph
yang sama (supaya `target_node_uuid`/`source_node_uuid` bisa saling merujuk).
Karena itu Fase 9 beroperasi di atas GRAPH GABUNGAN multi-ticker, bukan
langsung di atas satu `build_seed_from_ticker()` call — perlu satu langkah
merge tambahan (bagian dari Fase 9b, ilustratif di sini):

```python
# ILUSTRASI — Fase 9b, BUKAN implementasi di sini.
def build_universe_seed(tickers: List[str], as_of_date: str) -> FilteredEntities:
    # Langkah 1: setiap ticker tetap lewat jalur Fase 2 APA ADANYA, tidak diubah.
    per_ticker_seeds = [build_seed_from_ticker(t, as_of_date=as_of_date) for t in tickers]

    # Langkah 2: gabung entities jadi SATU FilteredEntities. uuid Company
    # sudah unik per ticker (f"{ticker}::company", konvensi Fase 2a) — tidak
    # ada tabrakan uuid antar seed, jadi concat aman tanpa dedup tambahan.
    merged_entities = [node for seed in per_ticker_seeds for node in seed.entities]
    merged = FilteredEntities(
        entities=merged_entities,
        entity_types={n.get_entity_type() for n in merged_entities if n.get_entity_type()},
        total_count=len(merged_entities),
        filtered_count=len(merged_entities),
    )

    # Langkah 3: Fase 9 — dedup artikel (bagian 1) -> LLM extraction (bagian 3)
    # -> merge edge lintas-artikel (bagian 2.5) -> MUTASI in-place related_edges/
    # related_nodes pada Company node yang relevan di `merged` (persis pola
    # entity_edge_builder.build_entity_edges: mutasi + return objek yang sama).
    build_news_entity_edges(merged, tickers, as_of_date)   # ilustratif, Fase 9b

    return merged
```

Konsekuensi eksplisit dari posisi "melengkapi" ini:
- Jalur SATU-ticker yang sudah ada (`build_seed_from_ticker` dipakai langsung
  oleh `portfolio_optimizer`, `oasis_profile_generator` untuk personas per
  ticker, endpoint `/api/portfolio/graph`) **sama sekali tidak terpengaruh** —
  mereka tidak pernah melihat graph gabungan/edge baru kecuali secara sengaja
  di-wire ke jalur baru ini (kemungkinan besar baru relevan mulai Fase 11b).
- uuid Company (`TICKER::company`, konvensi Fase 2a) dipakai APA ADANYA
  sebagai titik sambung — tidak perlu skema id baru.
- Bintang per-ticker Fase 2 tetap utuh DI DALAM graph gabungan — edge baru
  Fase 9 murni ditambahkan sebagai LAPISAN tambahan pada node Company yang
  sudah ada, bukan menggantikan `related_edges` yang sudah terisi dari Fase 2c
  (`entity_edge_builder.build_entity_edges` sudah dipanggil oleh
  `build_seed_from_ticker` SEBELUM Fase 9 menambahkan edge company-to-company
  — jadi Company node yang masuk ke `build_news_entity_edges` sudah punya
  `related_edges` berisi has_sector/has_metric/has_signal/has_screen; Fase 9
  APPEND edge company-to-company ke list yang sama, tidak reset seperti
  `build_entity_edges` melakukannya untuk edge-nya sendiri).

---

## 5. Menangani Ketiadaan Berita (`articles=[]`)

**Keputusan: ticker TETAP muncul di graph sebagai node — hanya TIDAK
mendapat edge Company-to-Company dari sisi kontribusi artikelnya sendiri.
TIDAK di-skip dari proses Fase 9 sama sekali.**

Alasan:
- Konsisten dengan filosofi Fase 8b sendiri: `articles=[]` adalah hasil VALID
  (ticker sepi berita, bukan error) — memperlakukannya sebagai "skip ticker"
  akan diam-diam mengulang kesalahan yang sudah eksplisit dihindari di Fase
  8b bagian 3 aturan no.4.
- Entitas bintang Fase 2 ticker itu (Sector, FundamentalMetric,
  TechnicalSignal, ShariaScreen) **sama sekali tidak bergantung pada
  berita** — semuanya tetap terbangun normal lewat `build_seed_from_ticker`
  yang tidak berubah. Men-skip ticker dari graph Fase 9 berarti kehilangan
  sinyal fundamental/teknikal/syariah yang valid, murni karena alasan yang
  tidak berkaitan (cakupan media).
- Rejected alternative — "skip ticker dari run Fase 9 sama sekali": ditolak
  karena secara diam-diam menyusutkan universe investasi berdasarkan
  popularitas media, bukan merit investasi — analog dengan alasan Fase 8b
  skenario (b) menegaskan artikel sedikit BUKAN sinyal negatif untuk
  small/mid-cap.

**Nuansa yang wajib didokumentasikan** (mudah dilewatkan): `articles=[]`
untuk ticker X berarti X TIDAK MENYUMBANG artikel apa pun ke dedup group
(bagian 1) — X tidak pernah jadi SUMBER pemanggilan LLM. Tapi X **masih bisa
menerima edge MASUK** kalau ticker LAIN dalam run yang sama punya artikel
yang menyebut X (mis. artikel milik AAPL menyebut X sebagai kompetitor, dan
X ada di allow-list ticker run ini) — LLM tetap boleh melaporkan relasi itu
selama X ada di allow-list. Jadi "isolated" di sini spesifik berarti "tidak
ada edge Company-to-Company yang BERASAL dari berita X sendiri", BUKAN
jaminan X benar-benar nol edge di graph — itu baru terjadi kalau TIDAK ADA
artikel MANAPUN dalam seluruh run yang menyebut X (skenario (b) di bagian 6).

**Konsekuensi ke Fase 10 (generate persona)**: ticker tanpa edge
Company-to-Company sama sekali (baik sebagai sumber maupun target) harus
diperlakukan sebagai KASUS NORMAL oleh Fase 10, bukan kegagalan — persona/
seed generation untuk ticker itu tetap berjalan penuh dari sinyal Fase 2
(fundamental, teknikal, sektor, syariah) yang memang tidak pernah bergantung
pada berita. Ini analog dengan cara `_derive_investor_stance` sudah
menangani `evidence=[]` secara graceful (return stance netral, bukan error)
ketika satu metrik kosong — pola yang sama harus diterapkan Fase 10 saat
membaca (atau tidak menemukan) edge Company-to-Company: nol edge berita =
"tidak ada sinyal sosial/kompetitif dari berita untuk ticker ini", bukan
kegagalan pipeline.

---

## 6. Skenario Konkret

### (a) 2 ticker (AAPL, MSFT) sama-sama disebut di 1 artikel yang sama

Setup: universe run untuk `as_of_date="2026-08-20"` mencakup (antara lain)
AAPL dan MSFT. `get_news_data("AAPL", as_of_date="2026-08-20")` mengembalikan
artikel `article_id=12345` ("Apple and Microsoft Team Up on AI Chips",
`related_ticker="AAPL"`). `get_news_data("MSFT", as_of_date="2026-08-20")`
JUGA mengembalikan artikel dengan `article_id=12345` yang PERSIS SAMA
(`related_ticker="MSFT"`) — karena Finnhub meng-index artikel yang sama untuk
kedua ticker.

Langkah manual:
1. **Dedup (bagian 1)**: `by_id[12345]` dibuat saat memproses AAPL
   (`related_tickers=["AAPL"]`), lalu di-union saat memproses MSFT ->
   `related_tickers=["AAPL", "MSFT"]`. Artikel ini masuk ke `DedupedArticle`
   list HANYA SEKALI.
2. **1 panggilan LLM** (bagian 3.1) untuk `article_id=12345`, dengan
   `allowed_tickers` mencakup AAPL & MSFT (keduanya ada di universe run ini).
   Anggap `summary` terisi (`text_used="summary"`).
3. LLM mengembalikan:
   ```json
   {
     "relations": [
       {
         "ticker_a": "AAPL", "ticker_b": "MSFT",
         "relation_type": "partner_of",
         "confidence": 0.93,
         "evidence_quote": "Apple and Microsoft today announced a joint venture to co-design custom AI datacenter chips."
       }
     ],
     "event": {
       "event_type": "partnership_announcement",
       "description": "Apple and Microsoft jointly announced a custom AI datacenter chip partnership.",
       "involved_tickers": ["AAPL", "MSFT"],
       "evidence_quote": "Apple and Microsoft today announced a joint venture to co-design custom AI datacenter chips."
     }
   }
   ```
4. **Verifikasi (bagian 3.3)**: `evidence_quote` dicek sebagai substring
   dari `headline + summary` artikel 12345 yang sebenarnya dikirim ke LLM —
   LOLOS (asumsi memang ada di teks aslinya). `relation_type` ada di enum.
   `ticker_a`/`ticker_b` ada di allow-list. `symmetric=True` di-DERIVE dari
   `_NEWS_EDGE_SPEC["partner_of"]` (bukan dari respons LLM — LLM tidak pernah
   diminta mengisinya, bagian 3.2). Relation LOLOS semua gerbang.
5. **Belum ada edge AAPL-MSFT sebelumnya di run ini** -> tidak ada merge,
   `merged_from_article_count=1`, kunci merge dipilih dari cabang
   `symmetric=True` (bagian 2.5) yaitu `(min("AAPL","MSFT"), max("AAPL",
   "MSFT"), "partner_of")` — meskipun untuk 1 artikel saja kunci ini belum
   teruji tabrakan dengan apa pun.
6. Edge ditulis ke KEDUA node (bagian 2.3) — bentuk final identik dengan
   contoh JSON di bagian 2.3 di atas (outgoing di `AAPL::company`, incoming
   di `MSFT::company`, `fact` sama persis, `attributes.evidence` berisi tepat
   satu entri untuk `article_id=12345`, `source_published_at` =
   `latest_evidence_published_at` = `"2026-08-20T14:30:00Z"` karena baru 1
   artikel/min=max).
7. Event `evt::12345::partnership_announcement` juga dibuat (bagian 2.2),
   dengan edge `involves_company` (pola sama seperti edge lain, tidak
   dirinci ulang di sini) dari kedua Company ke Event tersebut.

### (b) 1 ticker dengan `articles=[]` untuk `as_of_date` tertentu

Setup: universe run yang sama (`as_of_date="2026-08-20"`) juga mencakup
`XYZ` (small-cap sepi berita — persis skenario (b) Fase 8b).
`get_news_data("XYZ", as_of_date="2026-08-20")` mengembalikan
`{"success": true, "out_of_range": false, "articles": []}` — valid, bukan
error (Fase 8b bagian 3/4). Asumsikan TIDAK ADA artikel ticker lain manapun
dalam run ini yang menyebut XYZ.

Langkah manual:
1. **Dedup (bagian 1)**: saat memproses XYZ, `result["articles"]` kosong ->
   loop `for raw in result["articles"]` tidak mengeksekusi apa pun -> XYZ
   TIDAK menyumbang entri apa pun ke `by_id`. Tidak ada error, tidak ada
   cabang khusus — baris kode yang sama persis dieksekusi untuk XYZ maupun
   AAPL/MSFT (Fase 8b prinsip "tidak ada logika khusus untuk kasus sepi",
   dipertahankan di sini).
2. **Tidak ada pemanggilan LLM apa pun untuk konten dari XYZ** — karena tidak
   ada artikel dari XYZ yang masuk dedup group. (Kalau ticker LAIN
   menyebutnya, XYZ tetap bisa dapat edge MASUK — lihat catatan bagian 5;
   di skenario ini diasumsikan benar-benar tidak disebut sama sekali.)
3. **`build_universe_seed` (bagian 4)** tetap memanggil
   `build_seed_from_ticker("XYZ", as_of_date="2026-08-20")` seperti ticker
   lain — XYZ tetap masuk `merged.entities` dengan bintang Fase 2 penuh:
   `XYZ::company`, `XYZ::sector::...`, `XYZ::fundamental_metric::...` (yang
   terisi), `XYZ::technical_signal::...` (yang terisi), mungkin
   `XYZ::sharia_screen` — semuanya independen dari berita.
4. **Hasil akhir**: `XYZ::company.related_edges` berisi edge STRUKTURAL Fase
   2c (`has_sector`, `has_metric` x N, `has_signal` x N, mungkin
   `has_screen`) SEPERTI BIASA — tapi **nol edge dengan `edge_name` dari
   enum bagian 2.4** (`competitor_of`/`supplies_to`/dst). XYZ node ADA di
   graph gabungan, ikut discreening/portfolio construction seperti biasa;
   hanya "terisolasi" secara spesifik di LAPISAN Company-to-Company Fase 9.
5. **Konsekuensi Fase 10** (bagian 5): generator persona untuk XYZ berjalan
   normal dari entitas Fase 2 (fundamental/teknikal/sektor/syariah) —
   TIDAK boleh melempar error/butuh minimal-1-edge-berita sebagai prasyarat.
   Bio/persona yang dihasilkan untuk XYZ murni berbasis angka existing, tanpa
   konteks kompetitif/berita — sama seperti hari ini SEBELUM Fase 9 dibangun
   sama sekali (degradasi graceful, bukan kegagalan pipeline).

### (c) 2 artikel berbeda melaporkan arah `supplies_to` yang berlawanan untuk pasangan ticker yang sama

Setup: universe run yang sama (`as_of_date="2026-08-20"`) mencakup AAPL dan
`SUPP` (fiktif). Dua artikel BERBEDA (bukan duplikat — `article_id` beda)
lolos dedup (bagian 1):
- `article_id=555`, published `2026-07-01`: "AAPL confirms SUPP as a key
  chip supplier for its next lineup."
- `article_id=777`, published `2026-08-15`: "SUPP signs new component supply
  deal with AAPL" — ditulis ambigu oleh outlet berbeda; asumsikan LLM
  membaca ini sebagai "SUPP is the customer, AAPL supplies to SUPP" (arah
  TERBALIK dari artikel 555, entah karena artikel ini sungguh melaporkan
  arah terbalik, atau karena ekstraksi salah baca — Fase 9 tidak bisa
  membedakan keduanya, itulah intinya skenario ini).

Langkah manual:
1. **2 panggilan LLM terpisah** (1 artikel = 1 panggilan, bagian 3.1) —
   TIDAK saling melihat satu sama lain.
2. Artikel 555 menghasilkan relation `{ticker_a: "AAPL", ticker_b: "SUPP",
   relation_type: "supplies_to", confidence: 0.85, evidence_quote: "AAPL
   confirms SUPP as a key chip supplier"}` → **tapi baca teksnya**: SUPP
   adalah pemasok AAPL, jadi arah sebenarnya adalah `ticker_a="SUPP",
   ticker_b="AAPL"` (SUPP supplies_to AAPL) — asumsikan ekstraksi LLM benar
   di sini: `{"ticker_a": "SUPP", "ticker_b": "AAPL", "relation_type":
   "supplies_to", ...}`.
3. Artikel 777 menghasilkan `{"ticker_a": "AAPL", "ticker_b": "SUPP",
   "relation_type": "supplies_to", "confidence": 0.70, "evidence_quote":
   "SUPP signs new component supply deal with AAPL"}` — dari framing
   artikel ini LLM membaca AAPL sebagai pemasok (arah TERBALIK dari langkah
   2).
4. Kedua relation lolos verifikasi (bagian 3.3) secara independen — masing-
   masing quote memang ada di artikel sumbernya sendiri, `relation_type` ada
   di enum, ticker ada di allow-list. `symmetric=False` di-derive dari
   `_NEWS_EDGE_SPEC["supplies_to"]` untuk KEDUA relation.
5. **Kunci merge (bagian 2.5, cabang `symmetric=False`, TIDAK di-sort)**:
   - Relation dari artikel 555: kunci `("SUPP", "AAPL", "supplies_to")`.
   - Relation dari artikel 777: kunci `("AAPL", "SUPP", "supplies_to")`.
   Dua kunci ini BERBEDA (arah tidak disamakan) → **TIDAK di-merge** — dua
   entri terpisah, bukan satu edge dengan confidence gabungan.
6. **Deteksi kontradiksi**: kedua edge menunjuk pasangan ticker yang sama +
   `relation_type` sama tapi arah berlawanan → keduanya ditandai
   `conflicting_direction: true`, saling menunjuk lewat `conflicting_edge_ref`.

Hasil akhir — DUA edge tersimpan terpisah (bukan satu edge yang salah/
tergabung):

```python
# Edge dari artikel 555, di SUPP::company.related_edges (outgoing)
{
    "direction": "outgoing",
    "edge_name": "supplies_to",
    "fact": "SUPP has a reported supplies_to relationship with AAPL "
            "(SUPP is the supplier), evidenced by 1 article (2026-07-01).",
    "target_node_uuid": "AAPL::company",
    "attributes": {
        "relation_type": "supplies_to",
        "symmetric": False,
        "confidence": 0.85,
        "evidence": [{"article_id": 555, "published_at": "2026-07-01T00:00:00Z",
                       "quote": "AAPL confirms SUPP as a key chip supplier"}],
        "source_published_at": "2026-07-01T00:00:00Z",
        "latest_evidence_published_at": "2026-07-01T00:00:00Z",
        "as_of_date": "2026-08-20",
        "text_used": "headline_only",
        "merged_from_article_count": 1,
        "conflicting_direction": True,
        "conflicting_edge_ref": "AAPL::company",   # ujung edge yang berlawanan arah
    },
}

# Edge dari artikel 777, di AAPL::company.related_edges (outgoing) — ARAH TERBALIK,
# BUKAN edge yang sama dengan di atas
{
    "direction": "outgoing",
    "edge_name": "supplies_to",
    "fact": "AAPL has a reported supplies_to relationship with SUPP "
            "(AAPL is the supplier), evidenced by 1 article (2026-08-15).",
    "target_node_uuid": "SUPP::company",
    "attributes": {
        "relation_type": "supplies_to",
        "symmetric": False,
        "confidence": 0.70,
        "evidence": [{"article_id": 777, "published_at": "2026-08-15T00:00:00Z",
                       "quote": "SUPP signs new component supply deal with AAPL"}],
        "source_published_at": "2026-08-15T00:00:00Z",
        "latest_evidence_published_at": "2026-08-15T00:00:00Z",
        "as_of_date": "2026-08-20",
        "text_used": "headline_only",
        "merged_from_article_count": 1,
        "conflicting_direction": True,
        "conflicting_edge_ref": "SUPP::company",   # ujung edge yang berlawanan arah
    },
}
```

Fase 9 berhenti di sini — MELAPORKAN kontradiksi lewat kedua field di atas,
TIDAK menebak mana yang benar dan TIDAK merata-ratakan confidence-nya jadi
satu angka yang menyembunyikan bahwa sumbernya sebenarnya saling bertentangan.
Keputusan cara memakai dua edge yang bertentangan ini (mis. menurunkan bobot
keduanya, atau menyajikannya sebagai ketidakpastian eksplisit) ada di tangan
Fase 11b.

---

## Ringkasan Keputusan

| Poin | Keputusan |
|---|---|
| 1. Dedup | Dilakukan SEKALI per `as_of_date`, lintas SEMUA ticker dalam run, di langkah orkestrasi baru SEBELUM LLM dipanggil. Kunci = `article_id`. `related_tickers` di-union (insertion-order, bukan re-sort). |
| 2a. Struktur | EXTEND `FilteredEntities`/`EntityNode` existing — bukan struktur terpisah. |
| 2b. Entity baru | `Event` — tipe baru, tidak bisa dipetakan ke 6 tipe existing. Opsional per artikel. |
| 2c. Edge Company-to-Company | Field wajib: `relation_type` (enum tertutup 6 nilai), `symmetric` (DERIVED dari enum, bukan dari LLM — lihat 3b), `confidence`, `evidence` (>=1, wajib lolos verifikasi), `source_published_at` (= earliest evidence, min), `latest_evidence_published_at` (= latest evidence, max), `as_of_date`, `text_used`, `merged_from_article_count`, `conflicting_direction`/`conflicting_edge_ref` (lihat 2d). Bentuk edge dict identik konvensi Fase 2c. |
| 2d. Edge dari banyak artikel | Kunci merge BERGANTUNG pada `symmetric`: relasi simetris → `(min(ticker), max(ticker), relation_type)` (sorted, aman digabung); relasi asimetris → `(ticker_a, ticker_b, relation_type)` TIDAK disort (arah asli dipertahankan). Arah berlawanan pada pasangan+relation_type yang sama TIDAK digabung — disimpan sebagai 2 edge terpisah bertanda `conflicting_direction: true` saling menunjuk lewat `conflicting_edge_ref`, bukan dipilih salah satu atau dirata-rata. Evidence digabung, confidence digabung via probabilistic-OR (capped 0.98) — HANYA untuk edge yang benar-benar sama arahnya. |
| 3a. Granularitas panggilan LLM | 1 artikel unik (pasca-dedup) = 1 panggilan LLM. |
| 3b. Anti-halusinasi | Evidence quote wajib + diverifikasi sebagai substring literal dari teks sumber (headline+summary) yang benar-benar dikirim; allow-list ticker eksplisit; enum relation_type tertutup; `symmetric` TIDAK diminta dari LLM sama sekali — di-derive dari `_NEWS_EDGE_SPEC[relation_type]` setelah enum divalidasi, supaya tidak ada dua sumber kebenaran untuk properti yang seharusnya tetap; edge yang gagal SATU gerbang mana pun DIBUANG. |
| 3c. Caching ekstraksi | WAJIB, keyed by `article_id`, tidak pernah expire — bukan opsional, demi determinisme lintas run. |
| 4. Integrasi Fase 2 | MELENGKAPI — bintang Fase 2 tidak diubah. Fase 9 butuh graph gabungan multi-ticker (langkah merge baru) karena edge Company-to-Company menyeberangi hasil `build_seed_from_ticker` yang terpisah per ticker. |
| 5. `articles=[]` | Ticker TETAP di graph (bintang Fase 2 penuh), TIDAK di-skip dari Fase 9. Hanya tidak menyumbang edge Company-to-Company dari sisi berita sendiri (bisa tetap terima edge masuk dari artikel ticker lain). Fase 10 harus menangani nol-edge-berita sebagai kasus normal. |
| 0. Validasi empiris | WAJIB dijalankan sebagai langkah PERTAMA Fase 9b, sebelum kode ekstraksi LLM ditulis — sample >=50 artikel, ukur % yang menyebut >=2 entitas perusahaan di headline+summary; <10% memicu keputusan bersama, bukan lanjut diam-diam. |

**JANGAN mulai implementasi (Fase 9b) — dokumen ini menunggu review.**
