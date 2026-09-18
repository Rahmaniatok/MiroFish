# Fase 8a — Investigasi Sumber Data Berita (yfinance vs Finnhub)

Status: **investigasi selesai, implementasi belum dimulai** (menunggu review).

Konteks: Fase 8 (News Ingestion) akan melengkapi `backend/app/data_layer/` (saat ini
`market_data.py`, `cache.py`, `universe.py`) dengan data berita. Karena data layer lain
sudah mendukung backtest lewat `as_of_date` (lihat `get_stock_context(ticker, as_of_date)`),
sumber berita yang dipilih HARUS bisa difilter ke tanggal historis tertentu, bukan cuma
berita live saat dipanggil.

Script investigasi (scratch, boleh dihapus): [scripts/investigate_news.py](../../scripts/investigate_news.py).
Cara jalan:
```
cd backend && python ../scripts/investigate_news.py yfinance   # tanpa API key
cd backend && python ../scripts/investigate_news.py finnhub    # butuh FINNHUB_API_KEY di .env
```
Output mentah tersimpan di `scripts/_news_investigation_output/*.json` (tidak di-commit, murni untuk debugging).

Ticker yang diuji (7, campuran mega-cap ramai berita dan yang lebih sepi):
`AAPL, MSFT, NVDA, TSLA` (mega-cap, ramai) dan `KO, PFE, AVGO` (lebih sepi/kurang di-hype).

---

## 1. Tabel Perbandingan

| # | Pertanyaan | yfinance (`Ticker.get_news()`) | Finnhub (`/api/v1/company-news`) |
|---|---|---|---|
| 1 | Bisa query historis dengan rentang tanggal? | **TIDAK.** `get_news(count=10, tab="news")` tidak punya parameter tanggal sama sekali (diverifikasi dari source `yfinance/base.py:591-631` — request body hanya berisi `snippetCount` + ticker; endpoint-nya `queryRef=latestNews`, murni "stream berita terbaru"). Tidak ada cara meminta "berita ticker X pada tanggal Y". | **YA.** Endpoint `company-news?symbol=&from=&to=&token=` menerima rentang tanggal eksplisit dan benar-benar memfilternya — dibuktikan: query jendela 7 hari 1 tahun lalu mengembalikan hanya artikel di dalam jendela itu, dan query 1 hari tunggal di luar batas historis mengembalikan 0 artikel (bukan error, bukan data tak relevan). |
| 2 | Seberapa jauh ke belakang? | Tidak ada jaminan/kontrol — yang didapat cuma "N artikel terbaru saat ini". Dengan `count=50`, rentang tanggal artikel yang balik cuma **2–9 hari** ke belakang tergantung ticker (AAPL: 16–17 Sep 2026; PFE: 8–17 Sep 2026). Tidak reliable untuk backtest tanggal manapun di masa lalu — kalau `as_of_date` bukan "hari ini", yfinance **tidak bisa** menjawabnya sama sekali. | Diuji eksplisit dengan binary search pada AAPL (tanggal dasar 2026-09-17): **360 hari ke belakang (2025-09-22) masih ada data, 362 hari ke belakang (2025-09-20) sudah 0 artikel** — batas keras di sekitar **~360-361 hari (≈1 tahun berjalan)** untuk free tier. Pola yang sama (konvergen ke ~22 Sep 2025) terkonfirmasi di ke-7 ticker saat query jendela "1 tahun lalu". Untuk backtest lebih tua dari ~1 tahun, free tier Finnhub **tidak cukup** — perlu tier berbayar atau sumber lain. |
| 3 | Field yang tersedia | `id` (top), lalu `content.{id, contentType, title, description, summary, pubDate, displayTime, isHosted, bypassModal, previewUrl, thumbnail{...}, provider{displayName,url,sourceId}, canonicalUrl{...}, clickThroughUrl{...}, metadata{editorsPick}, finance{...}, storyline}`. **Tidak ada field ticker terkait (related tickers)** — artikel cuma "terhubung" ke ticker karena diambil lewat stream ticker itu. | `category, datetime, headline, id, image, related, source, summary, url`. Field `related` **bukan list ticker**, cuma string tunggal = ticker yang di-query (jadi juga tidak memberi info multi-ticker riil). Field `source` (mis. "Yahoo") ada, publisher lebih eksplisit daripada `provider.displayName` yfinance untuk sebagian item. |
| 4 | Format timestamp | ISO 8601 string UTC dengan suffix `Z`, mis. `"2026-09-17T10:04:00Z"` (field `pubDate`, juga `displayTime` identik di semua sampel). | Unix epoch integer (detik, UTC), mis. `1789606869` (field `datetime`). |
| 5 | Jumlah artikel per query | Dikontrol oleh param `count` (default 10; diuji `count=50` → semua 7 ticker mengembalikan **tepat 50**, dibatasi oleh permintaan, bukan oleh ketersediaan sebenarnya). | Ditentukan oleh rentang tanggal, bukan hard cap eksplisit. Jendela 30 hari terakhir: **210–250 artikel/ticker**. Jendela 7 hari (~1 tahun lalu): **18–158 artikel/ticker**, jauh lebih sedikit untuk ticker yang lebih sepi berita (KO=18) vs ramai (AAPL=108, NVDA=158). |
| 6 | Konsisten di 7 ticker? | **Ya, struktur field 100% identik** di AAPL/MSFT/NVDA/TSLA/KO/PFE/AVGO, semua mengembalikan 50/50 item, tidak ada yang kosong. | **Ya, struktur field 100% identik** di semua 7 ticker, tidak ada respons kosong pada jendela 30-hari-terakhir. Volume artikel bervariasi besar per popularitas ticker (ekspektasi wajar), bukan bug. Catatan kualitas: relevansi tidak selalu ketat — satu sampel PFE ternyata berita CEO baru Bavarian Nordic yang cuma menyebut "Pfizer" sekilas di riwayat karier eksekutif (lihat sampel di bawah) — filter `related` Finnhub tampaknya berbasis penyebutan/keyword, bukan tagging ketat. |
| 7 | Rate limit riil (bukan cuma dokumentasi) | Tidak terjadi throttling/429 pada burst 15 ticker beruntun tanpa delay (~0.1–0.2s/panggilan, total 2.3s). **Tidak ada header rate-limit** di respons sama sekali — artinya batasnya tidak transparan; dari reputasi publik Yahoo dikenal bisa mem-blok IP pada volume tinggi berkelanjutan (tidak diuji di sini karena berisiko memblokir IP investigasi ini). | Header `X-Ratelimit-Limit: 60` (per menit) muncul di **setiap** respons, terkonfirmasi berkurang linear (`X-Ratelimit-Remaining` 46→16 dalam burst 35 panggilan/18 detik ≈ 2 req/s) tanpa satupun 429. Cocok dengan dokumentasi publik (60 calls/menit untuk free tier) — dan di sini benar-benar diverifikasi lewat header live, bukan cuma dibaca dari docs. |

## 2. Sampel JSON Mentah

### yfinance — AAPL (item pertama dari `get_news(count=50)`)
```json
{
  "id": "06f8ab20-faeb-3b7b-b682-05d84b00562c",
  "content": {
    "id": "06f8ab20-faeb-3b7b-b682-05d84b00562c",
    "contentType": "STORY",
    "title": "Berkshire Hathaway Has 30.6% of Its Portfolio in These 2 Magnificent AI Stocks. Here's Why That's a Signal Worth Watching.",
    "description": "",
    "summary": "Berkshire Hathaway is known for holding stable, boring stocks, but it still has significant exposure to AI companies.",
    "pubDate": "2026-09-17T10:04:00Z",
    "displayTime": "2026-09-17T10:04:00Z",
    "isHosted": true,
    "bypassModal": false,
    "previewUrl": null,
    "thumbnail": {
      "originalUrl": "https://media.zenfs.com/en/motleyfool.com/1f8a2803afbc1aded89dfa1f244a1909.png",
      "originalWidth": 1200,
      "originalHeight": 800,
      "caption": "Apple logo beside the word Alphabet on a red and black background.",
      "resolutions": [
        { "url": "https://s.yimg.com/.../1200x800", "width": 1200, "height": 800, "tag": "original" },
        { "url": "https://s.yimg.com/.../170x128", "width": 170, "height": 128, "tag": "170x128" }
      ]
    },
    "provider": { "displayName": "Motley Fool", "url": "http://www.fool.com/", "sourceId": "motleyfool.com" },
    "canonicalUrl": { "url": "https://www.fool.com/investing/2026/09/17/...", "site": "finance", "region": "US", "lang": "en-US" },
    "clickThroughUrl": { "url": "https://finance.yahoo.com/technology/ai/articles/...", "site": "finance", "region": "US", "lang": "en-US" },
    "metadata": { "editorsPick": false },
    "finance": { "premiumFinance": { "isPremiumNews": false, "isPremiumFreeNews": false } },
    "storyline": null
  }
}
```
*(URL panjang dipotong untuk keterbacaan; file penuh ada di `scripts/_news_investigation_output/yfinance_AAPL.json`.)*

### Finnhub — AAPL (item pertama, jendela 30 hari terakhir)
```json
{
  "category": "company",
  "datetime": 1789606869,
  "headline": "Did AI Servers Really Quadruple Dell Stock?",
  "id": 142204461,
  "image": "https://s.yimg.com/rz/stage/p/yahoo_finance_en-US_h_p_finance_2.png",
  "related": "AAPL",
  "source": "Yahoo",
  "summary": "Dell Technologies (DELL) stock has returned 334% over the past year, against 16% for the S&P 500. The easy explanation is AI orders, but it is incomplete. The bigger change is what happened to the rest of Dell.",
  "url": "https://finnhub.io/api/news?id=4909ea4bada9f6a3b6849b062a4f75db9788952a90be9b4297a03fbd877fe0c0"
}
```

### Finnhub — PFE (contoh masalah relevansi longgar)
```json
{
  "category": "company",
  "datetime": 1789624500,
  "headline": "Tarja Stenvall Appointed Chief Executive Officer of Bavarian Nordic",
  "id": 142209009,
  "related": "PFE",
  "source": "Yahoo",
  "summary": "...Tarja Stenvall is a global healthcare executive with over 20 years of leadership experience from Sanofi, AstraZeneca, and Pfizer...",
  "url": "https://finnhub.io/api/news?id=4398563e87cc9458d32900e8f4a439bd26acea5cc90c586bb27fa9699f5d328b"
}
```
Berita ini sebenarnya tentang Bavarian Nordic, "Pfizer" cuma disebut di riwayat karier eksekutif — contoh nyata bahwa `related=PFE` dari Finnhub tidak berarti berita itu benar-benar *tentang* PFE.

## 3. Ringkasan Angka per Ticker

**yfinance** (`count=50`, semua tanggal adalah UTC, format `pubDate`):

| Ticker | n | oldest | newest |
|---|---|---|---|
| AAPL | 50 | 2026-09-16T02:00:00Z | 2026-09-17T10:04:00Z |
| MSFT | 50 | 2026-09-16T09:39:49Z | 2026-09-17T10:00:00Z |
| NVDA | 50 | 2026-09-16T13:51:14Z | 2026-09-17T10:05:00Z |
| TSLA | 50 | 2026-09-15T20:14:02Z | 2026-09-17T08:40:44Z |
| KO | 50 | 2026-09-11T11:37:08Z | 2026-09-17T10:00:33Z |
| PFE | 50 | 2026-09-08T20:54:15Z | 2026-09-17T09:04:00Z |
| AVGO | 50 | 2026-09-14T19:09:09Z | 2026-09-17T10:00:33Z |

**Finnhub**, jendela 30 hari terakhir (`from=2026-08-18, to=2026-09-17`):

| Ticker | n |
|---|---|
| AAPL | 244 |
| MSFT | 250 |
| NVDA | 246 |
| TSLA | 237 |
| KO | 210 |
| PFE | 220 |
| AVGO | 247 |

**Finnhub**, jendela 7 hari, ~1 tahun lalu (`from=2025-09-17, to=2025-09-24`) — semua ticker konvergen ke rentang aktual 2025-09-22 s/d 2025-09-24 (bukti batas ~360 hari, bukan kebetulan per-ticker):

| Ticker | n | rentang aktual |
|---|---|---|
| AAPL | 108 | 2025-09-22 .. 2025-09-24 |
| MSFT | 114 | 2025-09-22 .. 2025-09-24 |
| NVDA | 158 | 2025-09-22 .. 2025-09-24 |
| TSLA | 81 | 2025-09-22 .. 2025-09-24 |
| KO | 18 | 2025-09-22 .. 2025-09-24 |
| PFE | 52 | 2025-09-22 .. 2025-09-24 |
| AVGO | 49 | 2025-09-22 .. 2025-09-24 |

Probe batas historis (AAPL, hari tunggal, mundur dari 2026-09-17):

| Hari ke belakang | Tanggal | Artikel |
|---|---|---|
| 200 | 2026-03-01 | 19 |
| 300 | 2025-11-21 | 35 |
| 350 | 2025-10-02 | 35 |
| 358 | 2025-09-24 | 49 |
| 360 | 2025-09-22 | 11 |
| **362** | **2025-09-20** | **0** |
| 365 | 2025-09-17 | 0 |
| 400 | 2025-08-13 | 0 |

## 4. Kesimpulan

**Rekomendasi sumber utama: Finnhub**, dengan keterbatasan:
1. Free tier hanya menjangkau ~360 hari (≈1 tahun) ke belakang dari hari ini — backtest
   untuk `as_of_date` lebih tua dari itu **tidak akan mendapat berita** dari sumber ini
   sama sekali (perlu didokumentasikan sebagai limitation di data layer, pola yang sama
   seperti `universe_caveats`/`is_point_in_time=False` di Fase 1/8a sebelumnya — lihat
   memory `mirofish-phase8c-backtest-loop-requirements`).
2. Rate limit nyata 60 request/menit per API key — untuk fetch banyak ticker × banyak
   `as_of_date`, perlu batching/backoff eksplisit (bukan sekadar dokumentasi, sudah
   diverifikasi lewat header `X-Ratelimit-Remaining` yang benar-benar berkurang).
3. Field `related` bukan daftar ticker sungguhan, cuma echo dari ticker yang di-query, dan
   relevansi artikel tidak selalu ketat (contoh PFE di atas) — perlu treat sebagai
   "artikel yang menyebut ticker ini", bukan "artikel murni tentang ticker ini".
4. Butuh API key (gratis, sudah didaftarkan & disimpan di `.env` lokal — **tidak
   di-commit**).

**yfinance TIDAK direkomendasikan sebagai sumber utama** untuk kebutuhan backtest karena
`get_news()` murni "stream berita terbaru" tanpa parameter tanggal sama sekali — secara
struktural tidak bisa menjawab "berita ticker X pada `as_of_date` Y" untuk `as_of_date` di
masa lalu. Bisa dipertimbangkan sebagai sumber tambahan/live-only (mis. untuk tampilan
"berita terkini" di luar konteks backtest), tapi bukan untuk data layer yang harus
menghormati `as_of_date` seperti `market_data.py`/`universe.py` yang sudah ada.
