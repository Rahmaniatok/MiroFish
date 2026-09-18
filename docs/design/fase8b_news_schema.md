# Fase 8b — Desain Skema Penyimpanan Berita

Status: **desain, belum implementasi** (Fase 8c menunggu review dokumen ini).

Konteks: Fase 8a ([docs/investigations/fase8a_news_data.md](../investigations/fase8a_news_data.md))
memilih **Finnhub Company News** sebagai sumber, dengan dua keterbatasan nyata yang
harus tercermin di skema:
1. Free tier hanya bisa query mundur **~360 hari dari hari fetch dilakukan** (rolling
   window dari "hari ini", bukan tanggal tetap — lihat catatan di bagian 4).
2. Rate limit **60 request/menit** per API key.

Dokumen ini murni desain. Tidak ada file baru di `backend/app/data_layer/`, tidak ada
migration. Struct/dataclass di bawah ini **ilustratif** (ditulis di code block markdown),
bukan file `.py` yang bisa dijalankan.

Referensi pola existing yang dibaca (tidak diubah):
- `backend/app/data_layer/cache.py` — cache key `(ticker, as_of_date, data_type)`,
  TTL 30 menit untuk live, snapshot historis tidak pernah expire, upsert via
  DELETE-then-INSERT.
- `backend/app/data_layer/market_data.py` — pola `success`/`error`, `schema_version`
  untuk cache-busting, `is_point_in_time` + `warning` untuk data yang bukan point-in-time,
  leakage guard eksplisit untuk `as_of_date`.
- `backend/app/services/portfolio_agent.py` (~line 209-225) — pola caveat yang SUNGGUH
  ADA di branch ini: field nullable tunggal (`zero_weight_caveat`), diisi kondisional,
  berisi prosa instruktif lengkap (bukan dict caveats terpisah). Ini yang dipakai sebagai
  acuan di bagian 4, BUKAN `universe_caveats` — field itu ternyata tidak ada di branch
  `konsesus-lllm` saat diverifikasi ulang (lihat catatan koreksi di bagian akhir).

---

## 1. Struktur Data 1 Item Berita

Field mentah Finnhub (dari fase8a): `category, datetime, headline, id, image, related,
source, summary, url`. Tidak semua field mentah perlu disimpan — berikut keputusan
per field, plus field turunan baru:

| Field disimpan | Wajib/Nullable | Sumber | Alasan |
|---|---|---|---|
| `article_id` | **Wajib** | Finnhub `id` (int) | Kunci dedup — artikel yang sama bisa muncul lagi di query lain untuk `as_of_date` berbeda (window 90 hari saling tumpang tindih antar tanggal berdekatan), dan sejak jumlah artikel per window TIDAK dibatasi (lihat bagian 3), volume duplikasi potensial jauh lebih besar daripada kalau hasil dicap ke angka kecil. Tanpa `article_id`, penyimpanan gabungan lintas `as_of_date` tidak bisa dideduplikasi sama sekali. |
| `headline` | **Wajib** | Finnhub `headline` | Konten inti — tanpa ini item tidak berguna. Finnhub selalu mengisinya (dikonfirmasi di fase8a, tidak ada sampel kosong di 7 ticker). |
| `summary` | Nullable | Finnhub `summary` | Berguna untuk entity/consensus extraction (Fase 8c) tanpa harus fetch URL asli, tapi beberapa item historis (terutama dekat batas 360 hari) berpotensi punya summary kosong string — diperlakukan sebagai `None` kalau string kosong, bukan disimpan sebagai `""` (konsisten dengan cara `fetch_fundamental_data` men-skip nilai yang tidak reliable, bukan menyimpan sampah). |
| `publisher` | Nullable | Finnhub `source` | Untuk transparansi/atribusi (pola sama seperti `provider.displayName` di sampel mentah yfinance fase8a). Nullable karena tidak divalidasi 100% coverage-nya di fase8a (7 ticker semua terisi "Yahoo", tapi sampel kecil). |
| `url` | **Wajib** | Finnhub `url` | Diperlukan untuk citation/traceability kalau nanti dipakai sebagai bukti di debate room (pola Fase 4) atau ditampilkan ke user. Finnhub selalu mengisi ini. |
| `published_at` | **Wajib** | Finnhub `datetime` (unix epoch) **dikonversi ke ISO 8601 UTC string** saat disimpan | Disimpan sebagai string ISO (`"2026-09-15T14:30:00Z"`), BUKAN epoch mentah — supaya konsisten dengan `pubDate`/`as_of_date`/`fetched_at` yang semuanya ISO string di seluruh data layer existing (`cache.py` fetched_at, `market_data.py` as_of_date). Ini yang dipakai untuk aturan no-lookahead di bagian 3, jadi WAJIB ada dan WAJIB valid. |
| `related_ticker` | **Wajib** | Ticker yang di-query (bukan field mentah Finnhub) | Field mentah Finnhub `related` cuma echo dari ticker yang di-query, BUKAN daftar ticker sungguhan yang relevan (temuan eksplisit fase8a) — jadi kita simpan ulang sebagai `related_ticker` singular yang jelas maknanya "artikel ini muncul saat query ticker X", bukan "artikel ini tentang ticker X secara eksklusif". Nama field sengaja beda dari field mentah Finnhub supaya konsumen di Fase 8c tidak salah asumsi soal presisi relevansinya. |

**Field mentah Finnhub yang SENGAJA TIDAK disimpan** (dan alasannya):
- `category` — selalu `"company"` di sampel fase8a (tidak ada variasi teramati), tidak ada
  konsumen yang direncanakan memakainya; kalau nanti ternyata bervariasi dan berguna,
  gampang ditambahkan (schema_version akan naik, lihat bagian 4).
- `image` — URL thumbnail, tidak ada rencana pemakaian di pipeline manapun (Fase 2-6 semua
  berbasis teks/angka, bukan render gambar). Menyimpannya cuma menambah ukuran cache tanpa
  konsumen.

Struct ilustratif (bukan file `.py`):
```python
# ILUSTRASI — bukan implementasi
@dataclass
class NewsArticle:
    article_id: int                    # wajib, dedup key
    headline: str                      # wajib
    summary: Optional[str]             # nullable
    publisher: Optional[str]           # nullable
    url: str                           # wajib
    published_at: str                  # wajib, ISO 8601 UTC, dipakai untuk no-lookahead check
    related_ticker: str                # wajib, ticker yang di-query (BUKAN klaim relevansi Finnhub)
```

## 2. Cache Key & Granularitas

**Keputusan: tetap pakai pola key `(ticker, as_of_date, data_type="news")` yang sama
seperti `cache.py`, dengan value berupa JSON list ter-serialize, TANPA cap jumlah artikel**
— bukan satu baris per artikel. Ukuran list divariasikan bebas oleh data aslinya (lihat
bagian 3), dan konsekuensi ukurannya dihitung eksplisit di bawah (bukan diasumsikan aman).

Alasan (trade-off dipertimbangkan eksplisit):

| Opsi | Kelebihan | Kekurangan | Keputusan |
|---|---|---|---|
| **A. Satu row per `(ticker, as_of_date)`, value = JSON list artikel, tanpa cap** (dipilih) | Zero perubahan skema tabel `market_data_cache` (`data_json TEXT`) — `data_type="news"` langsung jalan di skema existing tanpa migration. Konsisten 100% dengan cara `technical_indicators` (list nested) sudah disimpan di dalam blob `price` yang sama. Baca/tulis atomik per `as_of_date` — cocok dengan pola akses Fase 8c (satu backtest date, satu fetch, satu cache hit/miss). | Tidak bisa query "semua artikel unik untuk ticker X, lintas semua as_of_date" tanpa deserialize+union manual (tidak ada rencana pemakaian seperti itu di fase manapun sampai sekarang). Artikel yang sama bisa tersimpan berkali-kali di row `as_of_date` berbeda (window 90 hari saling overlap) — REDUNDANSI DISENGAJA, dan sejak TIDAK ADA cap jumlah, redundansi ini punya biaya storage nyata yang dihitung di bagian 6 skenario (d), bukan "diabaikan" begitu saja. | ✅ DIPILIH, biaya storage diterima sadar (lihat skenario (d)) |
| **B. Satu row per artikel** (tabel baru `news_cache`, key `article_id` atau `(ticker, article_id)`) | Tidak ada duplikasi penyimpanan lintas `as_of_date`; query "semua berita ticker X" jadi trivial SQL; ukuran per row kecil dan seragam (tidak bergantung pada seberapa ramai ticker-nya). | Butuh migration/tabel baru (di luar scope dokumen ini — instruksi eksplisit melarang bikin migration). Butuh join/query tambahan untuk merekonstruksi "artikel untuk `as_of_date` Y" dari daftar artikel per-ticker, plus tabel mapping `(ticker, as_of_date) -> [article_id,...]` yang pada dasarnya menduplikasi apa yang opsi A sudah lakukan lewat JSON list. | ❌ Ditunda — inilah opsi yang seharusnya dipilih KALAU redundansi opsi A ternyata jadi masalah operasional nyata (bukan cuma dihitung di atas kertas); lihat kesimpulan skenario (d) di bagian 6. |

Redundansi opsi A dijelaskan: kalau `as_of_date=2024-06-01` dan `as_of_date=2024-06-02`
sama-sama query window 90 hari ke belakang, artikel yang published di 2024-05-01 bisa
muncul di KEDUA row cache (dua JSON blob berbeda, isi tumpang tindih ~89/90 hari). Ini
DITERIMA sebagai trade-off — snapshot historis tidak pernah expire (lihat `cache.py`), jadi
tidak ada biaya refetch berulang; biaya yang dibayar murni storage duplikat. **Berbeda dari
draf sebelumnya (yang mengasumsikan cap 10 artikel dan mengabaikan biaya ini), sekarang
TANPA cap biayanya dihitung eksplisit dengan angka nyata di skenario (d), bagian 6** —
kesimpulannya tetap DITERIMA untuk fase ini, tapi dengan alasan yang diverifikasi, bukan
diasumsikan kecil.

Cache row untuk `data_type="news"` (ilustratif, TIDAK ubah `_SCHEMA` di `cache.py`):
```python
# ILUSTRASI bentuk data_json yang disimpan di kolom existing `data_json`
# (skema tabel market_data_cache TIDAK berubah — cuma data_type baru "news")
{
    "ticker": "AAPL",
    "as_of_date": "2024-06-01",   # None kalau live (sama seperti price/fundamental)
    "success": True,
    "error": None,
    "schema_version": 1,           # lihat bagian 4
    "window_start": "2024-03-03",  # as_of_date - 90 hari
    "window_end": "2024-06-01",    # == as_of_date, no-lookahead boundary
    "out_of_range": False,         # lihat bagian 4
    "caveat": None,                # lihat bagian 4 (pola zero_weight_caveat)
    "articles": [ NewsArticle, ... ],   # 0..N item, TIDAK dibatasi, urut terbaru dulu
}
```

## 3. Window Pencarian & Aturan No-Lookahead — TANPA CAP JUMLAH

**Aturan eksplisit (WAJIB, sama level kepentingannya dengan leakage guard di
`fetch_price_data`):**

1. Query Finnhub dengan `from = as_of_date - 90 hari`, `to = as_of_date`.
2. Ambil **SEMUA artikel** yang dikembalikan Finnhub dalam rentang itu — **TIDAK ADA cap
   ke 10, 20, atau angka manapun**. Urutkan `published_at` descending (terbaru/paling
   dekat ke `as_of_date` di depan) murni untuk keterbacaan konsumen, bukan untuk memotong
   list.
3. **TIDAK BOLEH ADA** artikel dengan `published_at > as_of_date` yang lolos ke hasil
   akhir — ini bukan cuma soal parameter `to` yang dikirim ke Finnhub (yang sudah
   membatasi di sisi API), tapi WAJIB ada filter eksplisit di sisi kita sesudah data
   diterima, sama seperti pola "LEAKAGE GUARD (defense in depth)" yang sudah ada di
   `fetch_price_data` (`market_data.py` ~line 421-434: filter ulang manual meskipun
   parameter `end` API seharusnya sudah membatasi). Alasannya sama: tidak percaya penuh
   ke jaminan API pihak ketiga untuk sesuatu sekrusial no-lookahead-bias. Aturan ini
   berlaku SAMA PERSIS baik hasilnya 0 artikel maupun ratusan — tidak ada pengecualian
   berdasarkan volume.
4. **Hasil 0 artikel adalah VALID, bukan error**, dan **jumlah artikel BOLEH berapa pun**
   (0 sampai puluhan/ratusan, tergantung seberapa ramai ticker-nya dan seberapa dekat
   `as_of_date` ke batas jangkauan 360 hari) — ini BUKAN bug, murni refleksi volume berita
   riil dalam window tersebut. `success=True` tetap, `error=None` tetap, `articles`
   berisi persis apa yang ada, seberapa pun panjangnya (lihat skenario (a)/(b)/(d) di
   bagian 6 untuk rentang variasi ini, dan skenario (c) untuk membedakannya dari
   "di luar jangkauan" di bagian 4).

Kenapa 90 hari (bukan mengikuti pola window `lookback`/`period` price data yang macam-macam
seperti "1mo"/"1y"): dipilih SEBAGAI DEFAULT TUNGGAL (bukan parameter per-call seperti
`period` di `fetch_price_data`) karena kebutuhan Fase 8c hanya "konteks berita terbaru
sebelum tanggal backtest", bukan analisis tren berita jangka panjang — 90 hari cukup untuk
menangkap earnings call/pengumuman besar terakhir tanpa membebani rate limit 60/menit dengan
window yang lebih lebar per ticker. Kalau kebutuhan berubah, ini konstanta tunggal yang
gampang diubah, bukan API publik yang harus didesain fleksibel sejak awal.

## 4. TTL & Caveat Out-of-Range

**Field caveat: `caveat: Optional[str]`, mengikuti pola `zero_weight_caveat` yang
TERVERIFIKASI ADA di `portfolio_agent.py`** — bukan dict caveats terpisah, bukan
`universe_caveats` (field itu ternyata TIDAK ADA di branch `konsesus-lllm` saat
diverifikasi ulang untuk dokumen ini — lihat catatan koreksi di akhir dokumen).

Field pendamping `out_of_range: bool` (bukan cuma mengandalkan `caveat is not None`,
supaya konsumen bisa branch secara terstruktur tanpa parsing string) menentukan mana dari
dua kondisi "kosong" berikut yang sedang terjadi:

| Kondisi | `out_of_range` | `articles` | `caveat` |
|---|---|---|---|
| `as_of_date` valid, ada berita | `False` | 1..N item (tidak dibatasi) | `None` |
| `as_of_date` valid, TIDAK ADA berita dalam window 90 hari (memang sepi berita) | `False` | `[]` | `None` — list kosong SUDAH cukup jelas maknanya karena `out_of_range=False` menegaskan ini bukan soal jangkauan |
| `as_of_date` lebih tua dari ~360 hari sebelum hari fetch (di luar jangkauan Finnhub free tier) | `True` | `[]` | String prosa instruktif (lihat di bawah) |

Isi `caveat` saat `out_of_range=True` (ilustratif, mengikuti gaya `zero_weight_caveat` —
menjelaskan apa yang BUKAN penyebab, lalu apa yang SUNGGUH terjadi):
```
"Tidak ada artikel Finnhub yang dikembalikan untuk as_of_date=2023-01-15 karena tanggal
ini berada DI LUAR jangkauan historis free tier Finnhub (~360 hari mundur dari tanggal
fetch dilakukan, 2026-09-18 saat cache ini ditulis — bukan tanggal tetap, lihat catatan
'rolling window' di bawah). Ini BUKAN berarti tidak ada berita nyata tentang ticker ini
pada tanggal tersebut, dan BUKAN kegagalan API/rate-limit — ini keterbatasan cakupan
Finnhub free tier. Jangan diinterpretasikan sebagai 'tidak ada berita relevan' oleh
konsumen downstream (mis. entity extraction/consensus scoring Fase 8c); perlakukan sebagai
NULL/tidak diketahui, bukan sinyal negatif."
```

**Penting soal "rolling window, bukan tanggal tetap"**: karena batas ~360 hari dihitung
dari HARI FETCH DILAKUKAN (bukan hari ini di luar konteks), dua fetch untuk `as_of_date`
yang SAMA persis bisa menghasilkan `out_of_range` yang BERBEDA kalau dilakukan di hari
kalender yang berbeda (mis. di-fetch hari ini → di luar jangkauan; di-fetch 30 hari lagi
untuk `as_of_date` yang sama → makin jauh di luar jangkauan, TIDAK PERNAH berbalik jadi
dalam jangkauan). Konsekuensi desain: begitu satu `(ticker, as_of_date)` sukses ter-cache
dengan `out_of_range=False` (baik ada isinya maupun `[]`), cache itu valid selamanya
(snapshot historis, sama seperti price/fundamental). Tapi kalau ter-cache dengan
`out_of_range=True`, hasil itu JUGA valid selamanya untuk disimpan (tanggalnya makin lama
makin pasti tetap di luar jangkauan, tidak pernah "sembuh") — TIDAK PERLU re-fetch berkala.
Ini beda dari live-data TTL: `out_of_range=True` bukan "data kedaluwarsa", tapi "jawaban
permanen: sumber ini tidak bisa menjawab untuk tanggal ini".

**TTL untuk hasil yang berhasil (bukan out-of-range):**
- `as_of_date` diisi (historis) → **tidak pernah expire**, pola identik dengan
  `cache.py`/`get_price_data`/dst — snapshot masa lalu tidak berubah.
- `as_of_date=None` (mode live, "berita terbaru sampai sekarang") → **TTL 30 menit**,
  SAMA PERSIS dengan `LIVE_DATA_TTL_MINUTES` existing di `cache.py`. Tidak perlu ambang
  baru — berita bergerak secepat/lebih cepat dari harga saham, jadi window TTL live yang
  sudah ada (dirancang untuk data yang berubah cepat) sudah tepat, tidak perlu dibuat lebih
  longgar.
- **Tidak ada ambang "dekat hari ini" terpisah** untuk mode `as_of_date` terisi — begini
  alasannya: berbeda dari harga saham (yang punya konsep intraday "masih trading hari
  ini"), begitu `as_of_date` adalah TANGGAL SPESIFIK di masa lalu (termasuk kemarin), berita
  yang published pada/sebelum tanggal itu tidak akan berubah lagi — bahkan `as_of_date` =
  kemarin pun sudah "final" begitu tanggal itu lewat. Ambang tambahan di sini cuma akan
  menduplikasi konsep `as_of_date=None` yang sudah menangani kasus "benar-benar
  live/berjalan".

## 5. Rate Limit di Level Desain

**Catatan penting dulu, sebelum keputusan di bawah**: menghapus cap jumlah artikel
(bagian 3) **TIDAK menambah jumlah API call** ke Finnhub. Satu `(ticker, as_of_date)`
tetap = satu HTTP request ke `/company-news` — Finnhub tidak paginasi endpoint ini,
dikonfirmasi langsung di fase8a (query 30-hari AAPL mengembalikan 244 artikel dalam SATU
respons, bukan berhalaman). Jadi budget 60 req/menit dipakai berdasarkan jumlah
`(ticker, as_of_date)` yang di-fetch, sama sekali tidak terpengaruh oleh berapa banyak
artikel yang ternyata ada di dalam window itu.

**Keputusan: field tracking "kapan terakhir di-fetch per ticker" TIDAK masuk skema
data — itu murni urusan implementasi Fase 8c**, dengan alasan:

1. Cache existing sudah punya `fetched_at` per row (lihat `_SCHEMA` di `cache.py`,
   kolom `fetched_at TEXT NOT NULL`) — ini SUDAH cukup untuk keperluan audit/debug "kapan
   row ini terakhir ditulis". Kolom ini otomatis ada untuk `data_type="news"` juga TANPA
   perubahan skema tabel, karena `data_type` cuma nilai string baru di kolom existing.
2. Throttling 60 req/menit adalah keputusan **orkestrasi saat runtime** (mis. sleep antar
   batch, semaphore, atau antrian) — bukan sesuatu yang butuh disimpan sebagai state
   persisten per ticker. Kalau proses fetch berhenti di tengah jalan (crash/restart),
   throttle counter di memori hilang begitu saja dan itu BENAR — tidak ada state limit
   yang perlu "dilanjutkan", limitnya per-menit dan reset sendiri oleh Finnhub di sisi
   mereka (dikonfirmasi header `X-Ratelimit-Remaining` di fase8a).
3. Menambahkan field seperti `last_fetch_attempt_at` ke skema data justru mencampur dua
   concern yang beda: "apa isi cache ini" (data) vs "bagaimana cara mengisinya secara
   sopan ke API pihak ketiga" (orkestrasi/rate-limiting). `cache.py` sendiri sudah
   memisahkan ini — `fetched_at` mencatat SUKSES terakhir, bukan SETIAP percobaan
   (termasuk yang gagal karena 429), dan pemisahan itu tetap dipertahankan di sini.
4. Kalau Fase 8c nanti butuh resume/checkpoint eksplisit untuk batch fetch besar
   (misal 500 ticker × N `as_of_date` dalam satu backtest run), itu state EKSEKUSI
   sebuah RUN backtest (mis. daftar `(ticker, as_of_date)` yang sudah selesai vs belum),
   bukan state PER-ARTIKEL/PER-CACHE-ROW — desain untuk itu ada baiknya jadi bagian
   desain loop Fase 8c sendiri (lihat memory constraint dari audit Phase 8a soal
   konsistensi `as_of_date` per run), bukan menumpang di skema cache berita ini.

## 6. Skenario Konkret

### (a) `as_of_date` valid, berita ditemukan dalam jumlah wajar (5-8 artikel)
Input hipotetis: `ticker="XYZ"` (mid-cap kurang populer — bukan salah satu dari 7 ticker
fase8a, semuanya S&P mega/large-cap dengan volume tinggi; ini ilustrasi untuk mengisi
"kelas menengah" di antara skenario (b) yang sangat sepi dan (d) yang sangat ramai),
`as_of_date="2026-08-20"` (dalam jangkauan 360 hari).

Langkah manual:
1. Window = `[2026-05-22, 2026-08-20]`.
2. Query Finnhub → misal 7 artikel ditemukan dalam window (ticker cukup diliput tapi
   bukan nama yang setiap hari ada beritanya).
3. Filter `published_at <= 2026-08-20` (no-lookahead guard) — tidak ada yang terbuang
   kalau `to` API sudah benar, tapi filter tetap jalan sebagai defense-in-depth.
4. TIDAK ada pemotongan jumlah (bagian 3) — ke-7 artikel semuanya disimpan.

Output:
```json
{
  "ticker": "XYZ", "as_of_date": "2026-08-20", "success": true, "error": null,
  "schema_version": 1,
  "window_start": "2026-05-22", "window_end": "2026-08-20",
  "out_of_range": false, "caveat": null,
  "articles": [ /* 7 item, urut published_at descending */ ]
}
```

### (b) `as_of_date` valid tapi cuma ada 1-2 artikel dalam window 90 hari
Input hipotetis: `ticker="ABC"` (small-cap sangat jarang diliput media), atau bisa juga
salah satu dari 7 ticker fase8a tapi pada `as_of_date` yang jatuh di zona penipisan data
dekat batas 360 hari (lihat tabel probe di fase8a: hari ke-360 mundur cuma 11 artikel
untuk AAPL padahal AAPL biasanya ramai — coverage Finnhub sendiri menipis mendekati
batas, bukan cuma soal ticker sepi berita). `as_of_date="2026-08-20"`, dalam jangkauan.

Langkah manual: sama seperti (a), tapi hasil filter cuma menghasilkan 2 item karena
memang cuma segitu yang published dalam window 90 hari — TIDAK ADA logika khusus untuk
kasus ini, jalur kodenya identik dengan (a) dan (d), cuma datanya yang beda.

Output:
```json
{
  "ticker": "ABC", "as_of_date": "2026-08-20", "success": true, "error": null,
  "schema_version": 1,
  "window_start": "2026-05-22", "window_end": "2026-08-20",
  "out_of_range": false, "caveat": null,
  "articles": [ /* cuma 2 item — VALID, bukan error, lihat aturan no.4 di bagian 3 */ ]
}
```
Konsumen (mis. entity extractor Fase 8c) melihat `len(articles)=2` tanpa perlu penjelasan
tambahan — `out_of_range=false` + `caveat=null` sudah menegaskan ini bukan keterbatasan
sumber data, cuma memang sedikit berita di window itu.

### (c) `as_of_date` di luar jangkauan 360 hari
Input: `ticker="AAPL"`, `as_of_date="2023-01-15"`, fetch dilakukan hari ini (2026-09-18)
→ `as_of_date` adalah ~1345 hari ke belakang, jauh melewati batas ~360 hari.

Langkah manual:
1. Hitung `days_back = (fetch_date - as_of_date).days` = ~1345.
2. `1345 > 360` (ambang, lihat catatan rolling-window di bagian 4) → tandai
   `out_of_range=True` SEBELUM memanggil Finnhub sama sekali (hemat 1 API call dari
   budget 60/menit — tidak ada gunanya query yang sudah pasti kosong).
3. `articles = []`, `caveat` diisi prosa instruktif (lihat bagian 4).

Output:
```json
{
  "ticker": "AAPL", "as_of_date": "2023-01-15", "success": true, "error": null,
  "schema_version": 1,
  "window_start": null, "window_end": null,
  "out_of_range": true,
  "caveat": "Tidak ada artikel Finnhub yang dikembalikan untuk as_of_date=2023-01-15 karena tanggal ini berada DI LUAR jangkauan historis free tier Finnhub (~360 hari mundur dari tanggal fetch dilakukan, 2026-09-18 saat cache ini ditulis...) [lihat teks lengkap di bagian 4]",
  "articles": []
}
```
Catatan: `success=true` (bukan `false`) karena ini BUKAN kegagalan teknis — permintaan
valid, jawabannya memang "di luar jangkauan sumber ini", pola yang sama seperti
`fundamental["is_point_in_time"]=False` yang tetap `success=True` di `market_data.py`
(keterbatasan sumber data ditandai lewat field khusus, bukan lewat `success=False`).
`window_start`/`window_end` sengaja `null` (bukan dihitung) karena query ke Finnhub tidak
pernah dilakukan sama sekali di langkah 2 — menyimpan window yang "seharusnya" dipakai
tapi tidak pernah benar-benar di-query berisiko menyesatkan pembaca cache row ini.

### (d) Ticker ramai berita — window 90 hari menghasilkan puluhan/ratusan artikel

Input: `ticker="AAPL"`, `as_of_date="2026-08-20"`, jauh dari batas 360 hari (jadi window
90 hari penuh benar-benar ter-cover, tidak menipis seperti skenario (b)).

**Estimasi volume (order-of-magnitude, BUKAN diukur langsung — fase8a hanya menguji
jendela 30 hari, bukan 90 hari):** fase8a mengukur AAPL 30-hari-terakhir = 244 artikel.
Volume berita tidak linear sempurna terhadap panjang window (ada hari sepi/ramai), tapi
sebagai estimasi kasar order-of-magnitude, window 90 hari untuk ticker seramai AAPL
masuk akal berada di kisaran **beberapa ratus artikel** (bukan angka presisi — poin
pentingnya adalah ini JAUH melebihi skenario (a)/(b), bukan angka pastinya).

**Apakah ini menimbulkan masalah desain? Dicek dua sisi secara eksplisit:**

1. **Ukuran satu cache row (bukan masalah).** Satu artikel ter-JSON-encode (article_id +
   headline + summary + publisher + url + published_at + related_ticker, lihat struct
   bagian 1) kira-kira 400-600 byte termasuk overhead JSON (nama field, tanda kutip,
   koma). Untuk ~700 artikel (batas atas estimasi di atas): **~700 × 500 byte ≈ 350 KB**
   per row. Kolom `data_json TEXT` di SQLite tidak punya limit praktis di skala ini
   (SQLite mendukung TEXT/BLOB hingga ~1 GB per nilai) — 350 KB per row sama sekali bukan
   masalah untuk SQLite lokal. Waktu serialize/deserialize JSON untuk beberapa ratus dict
   flat seperti ini juga berorde milidetik tunggal di Python — jauh lebih cepat daripada
   round-trip network yang sudah dilakukan untuk mengambil datanya dari Finnhub. **Bukan
   bottleneck.**
2. **Redundansi storage lintas `as_of_date` (masalah nyata, dihitung eksplisit — bukan
   diabaikan seperti draf sebelumnya).** Karena Opsi A di bagian 2 menyimpan JSON list
   penuh per `(ticker, as_of_date)`, dan window 90-hari antar `as_of_date` yang
   berdekatan saling tumpang-tindih ~89/90 hari, SATU artikel yang sama bisa tersimpan
   ulang di puluhan row cache berbeda. Untuk AAPL (~350 KB/row) di-fetch untuk setiap
   hari trading dalam satu backtest setahun (~252 hari): **252 × 350 KB ≈ 88 MB** hanya
   dari redundansi window yang tumpang tindih, UNTUK SATU TICKER SAJA. Untuk universe
   screening berisi puluhan-ratusan ticker (campuran ramai dan sepi), total storage bisa
   masuk ke skala GB, didominasi ticker-ticker ramai seperti AAPL/MSFT/NVDA.

**Keputusan: tetap terima Opsi A (JSON blob per row, tanpa tabel baru) untuk fase ini**,
dengan alasan:
   - Ini disk space di SQLite lokal (`market_cache.db`), bukan resource mahal/terbatas —
     pola yang sama (widen-then-trim di `fetch_price_data`, refetch fundamental penuh per
     row) sudah ada di data layer existing dan tidak pernah dioptimalkan untuk efisiensi
     storage.
   - Menghindari redundansi ini SUNGGUH-SUNGGUH butuh Opsi B (tabel `news_cache` terpisah,
     key per-artikel) — yang secara eksplisit di luar scope dokumen ini (dilarang bikin
     migration/tabel baru).
   - GB-scale file SQLite lokal masih wajar untuk tahap MVP/backtest riset. **Trigger
     untuk merevisit ke Opsi B**: kalau ukuran `market_cache.db` sungguh jadi masalah
     operasional nyata (disk penuh, query jadi lambat) — bukan dioptimalkan lebih dulu
     berdasarkan angka estimasi di atas kertas ini.

## 7. `schema_version` — Cache-Busting

Mengikuti pola `_FUNDAMENTAL_SCHEMA_VERSION`/`_SHARIA_SCHEMA_VERSION` persis: konstanta
int module-level (misal `_NEWS_SCHEMA_VERSION = 1`), dinaikkan setiap kali BENTUK/MAKNA
field cache berubah (bukan cuma penambahan field baru yang backward-compatible). Cached
row dengan `schema_version` lama diperlakukan sebagai cache miss dan di-refetch — mencegah
kode baru membaca field yang belum ada atau field yang maknanya sudah berubah dari row lama.

---

## Catatan Koreksi (ditemukan saat menyusun dokumen ini)

Memory sesi sebelumnya menyebut `screen_universe()` mengembalikan `universe_caveats` dan
ada konstanta `UNIVERSE_CAVEATS` di `universe.py`. Saat diverifikasi ulang untuk dokumen
ini (grep langsung ke `backend/app/**/*.py` di branch `konsesus-lllm`), **field/konstanta
itu tidak ada**. Kemungkinan itu hasil sesi di branch lain yang belum merge. Desain caveat
di dokumen ini (bagian 4) memakai pola yang SUNGGUH terverifikasi ada, yaitu
`zero_weight_caveat` di `portfolio_agent.py`. Memory yang bersangkutan sudah dikoreksi.
