# Fase 9a (baru) — Edge Company-to-Company Berbasis Metrik (Cluster Graph)

Status: **desain, belum implementasi** (Fase 9b baru menunggu review dokumen
ini — JANGAN mulai implementasi setelah dokumen ini selesai).

## Kenapa dokumen ini ada

Versi Fase 9 sebelumnya ([fase9a_entity_extraction_schema.md](fase9a_entity_extraction_schema.md),
sekarang **SUPERSEDED**) mendesain edge Company-to-Company dari ekstraksi LLM
atas berita (Fase 8). Validasi empiris wajib (bagian "0" dokumen lama) yang
dijalankan atas data Finnhub nyata (2.559 artikel unik, 12 ticker mega-cap)
menemukan **cross-mention rate cuma 5.4%** — di bawah ambang 10% yang sudah
disepakati sebagai syarat lanjut. Implementasi belum sempat ditulis (berhenti
di langkah validasi), jadi tidak ada rollback kode.

**Fase 9 (baru) mengganti pendekatan itu TOTAL**: edge Company-to-Company
diturunkan dari METRIK yang sudah ada di data layer (harga historis,
fundamental, sektor) — **TIDAK ADA LLM sama sekali** di seluruh dokumen ini.
Berita (Fase 8) TETAP dipakai di pipeline, tapi perannya bergeser sepenuhnya
ke **Fase 10 (generate persona)** dan **Fase 11 (isi debat OASIS)** — sama
sekali tidak lagi jadi input Cluster Graph.

**Scope dokumen ini murni desain.** Tidak ada file baru/berubah di
`backend/app/services/` atau `backend/app/data_layer/`. Referensi yang dibaca
(tidak diubah): `portfolio_optimizer.py` (pola perhitungan return/korelasi
yang SUDAH ADA), `market_data.py` (field fundamental persis), dan
`financial_entity_extractor.py` + `entity_edge_builder.py` (konvensi
entity/edge existing, termasuk gaya dict `_EDGE_SPEC`).

---

## 1. Sumber Data per Jenis Edge — REUSE, Bukan Fetch Baru

Ketiga jenis edge murni komputasi di atas data yang SUDAH difetch oleh Fase 1
— tidak ada panggilan API baru sama sekali.

### 1.1 Korelasi harga

**Input**: `build_returns_matrix(tickers, as_of_date, lookback="1y")` di
`portfolio_optimizer.py` — dipakai UTUH APA ADANYA, bukan ditulis ulang.
Fungsi ini sudah menghasilkan matriks return harian gap-free (ticker sebagai
kolom, tanggal sebagai baris), sumbernya `get_price_data`/`fetch_price_data`
(Fase 1, lookahead-safe), dengan aturan drop-ticker (riwayat kurang/insufficient
coverage) yang sudah teruji.

**Rentang periode**: **disamakan dengan default `portfolio_optimizer.py`,
yaitu `lookback="1y"`** (bukan window baru) — alasannya bukan sekadar
konsistensi kosmetik: `build_returns_matrix` DIPAKAI ULANG langsung (bukan
dipanggil ulang dengan parameter beda), jadi kalau Fase 9 nanti dipanggil
dalam alur yang sama dengan `optimize_portfolio` untuk `as_of_date` yang sama,
matriks return yang sama persis bisa dipakai bersama tanpa fetch price kedua
kalinya. Kalau Fase 9b nanti butuh window beda, itu keputusan terpisah yang
sadar — bukan default diam-diam.

**Yang BARU dihitung di Fase 9 (bukan reuse)**: `portfolio_optimizer.py`
TIDAK pernah menghitung matriks korelasi Pearson biasa — yang dipakainya
adalah **Ledoit-Wolf shrinkage covariance** (`EmpiricalPrior(covariance_estimator=LedoitWolf())`)
sebagai input optimizer, yang secara sengaja "menyusutkan" nilai
covariance/korelasi ke arah rata-rata untuk stabilitas optimasi portfolio —
BUKAN angka yang cocok dipakai sebagai "seberapa mirip pergerakan dua saham
ini" untuk keperluan graph (shrinkage akan meredam korelasi tinggi yang
sebenarnya asli demi mengurangi overfitting portfolio, sesuatu yang justru
TIDAK diinginkan kalau tujuannya melaporkan hubungan riil). Fase 9 menghitung
**Pearson correlation biasa** (`returns.corr()`, standar pandas) di atas
`returns` DataFrame yang SAMA yang dikembalikan `build_returns_matrix` —
input dipakai ulang, tapi hasil hitungnya beda tujuan dari yang dipakai
optimizer.

### 1.2 Kemiripan fundamental

**Input**: `get_fundamental_data(ticker, as_of_date)` (`market_data.py`),
field NUMERIK persis yang sudah dipetakan `financial_entity_extractor.py`
(`_VALUATION_METRIC_SPECS` + `_FUNDAMENTAL_METRIC_SPECS`), union 9 metrik:

| Metrik | Field | Unit asli |
|---|---|---|
| P/E | `pe_ratio` | rasio |
| P/B | `pb_ratio` | rasio |
| Market Cap | `market_cap` | USD (bisa $1B – $3T+, lihat normalisasi di bawah) |
| Revenue Growth YoY | `revenue_growth_yoy` | fraction |
| Profit Margin | `profit_margin` | fraction |
| ROE | `roe` | fraction |
| Debt/Equity | `debt_to_equity` | rasio |
| EPS Growth YoY | `eps_growth` | fraction |
| Dividend Yield | `dividend_yield` | fraction |

**Normalisasi — WAJIB, dijelaskan eksplisit langkah demi langkah**:

1. **`market_cap` di-log10-kan DULU**, sebelum z-score: `log_market_cap =
   log10(market_cap)`. Alasan: market cap rentang 3-4 order-magnitude ($1B –
   $3T+) — z-score di atas nilai dolar mentah akan didominasi total oleh
   mega-cap, membuat 8 metrik lain praktis tidak berpengaruh ke jarak
   Euclidean. Log-transform adalah langkah WAJIB, bukan opsional.
2. **Z-score per metrik, DIHITUNG DI SELURUH UNIVERSE RUN** (persis
   permintaan task) — untuk tiap metrik `m`, `z(ticker, m) = (value(ticker, m)
   - mean_m) / std_m`, dengan `mean_m`/`std_m` dihitung HANYA dari ticker yang
   PUNYA nilai non-null untuk metrik `m` di run ini (bukan seluruh S&P 500
   historis, bukan konstanta hardcode — populasi z-score adalah universe yang
   sedang diproses, supaya "mirip" berarti "mirip relatif terhadap peer di run
   ini", bukan relatif ke suatu baseline global yang tidak diverifikasi).
3. **Winsorize z-score ke [-3, +3]** sebelum dipakai di perhitungan jarak —
   pengaman standar supaya satu outlier ekstrem (mis. P/E negatif/sangat
   tinggi pada perusahaan nyaris tidak profitable) tidak mendominasi jarak
   untuk SEMUA pasangan yang melibatkan ticker itu.

**Metode jarak**: **RMS distance** (root-mean-square, BUKAN raw Euclidean
sum) atas z-score yang overlap antar dua ticker:

```
rms_distance(X, Y) = sqrt( mean( (z(X, m) - z(Y, m))^2  for m in overlap(X, Y) ) )
```

Kenapa RMS (dibagi jumlah metrik overlap), bukan `sqrt(sum(...))` biasa:
karena jumlah metrik yang overlap antar pasangan BISA BEDA (bagian 7 — data
fundamental parsial), dan `sqrt(sum(...))` tanpa pembagian akan secara
struktural menghasilkan jarak lebih besar untuk pasangan dengan LEBIH BANYAK
metrik overlap, terlepas dari seberapa mirip sebenarnya — itu artefak jumlah
dimensi, bukan sinyal kemiripan. Membagi dengan jumlah metrik overlap
(rata-rata, bukan jumlah) membuat jarak antar pasangan bisa dibandingkan adil
apa pun jumlah metrik yang tersedia untuk masing-masing.

`similarity_score` (dilaporkan di edge, bagian 4) = `1 / (1 + rms_distance)`
— transform standar jarak→skor ke rentang `(0, 1]`, 1.0 = identik persis di
semua metrik overlap, menurun monoton seiring jarak membesar.

**Minimum overlap WAJIB: >= 3 metrik.** Pasangan dengan < 3 metrik overlap
di-SKIP dari perbandingan fundamental sama sekali (bukan dipaksa dihitung
dari 1-2 metrik yang noise) — lihat bagian 7 untuk detail penanganan data
parsial. Ambang 3 dipilih karena kira-kira sama orde dengan jumlah metrik
yang sudah dipakai pola existing untuk SATU klaim stance (mis. archetype
Value di `_derive_investor_stance` cuma pakai 2 metrik P/E+P/B untuk satu
klaim) — 3 metrik dianggap cukup untuk klaim multi-faktor tanpa terlalu ketat
sampai mayoritas pasangan ter-skip.

### 1.3 Sektor

**Input**: entity `Sector` Fase 2 — persis `_extract_sector_entity` di
`financial_entity_extractor.py`, field `gics_sector` (dari
`fundamental["sector"]`) dan `industry` (dari `fundamental["industry"]`,
granularitas SUB-INDUSTRY, lebih halus dari `gics_sector`). **Tidak ada data
baru** — dua field ini SUDAH ada di entity yang sudah dibangun Fase 2a,
Fase 9 cukup membacanya.

---

## 2. Threshold / Top-K per Jenis Edge — Angka Konkret

### 2.1 Korelasi harga — Top-K=5 per ticker (BUKAN threshold tetap)

**Keputusan**: top-K, K=5, per ticker — BUKAN `|korelasi| >= X` tetap.

Alasan menolak threshold tetap: korelasi antar saham SANGAT bergantung rezim
pasar. Threshold `|corr| >= 0.7` di rezim high-correlation (mis. saat
sell-off makro serentak) bisa membuat graph nyaris lengkap (ratusan ribu
edge); di rezim low-correlation (pasar tenang, dispersi tinggi) threshold
yang sama bisa menghasilkan HAMPIR NOL edge. Top-K per ticker BEBAS dari
masalah ini — jumlah edge yang keluar dari mekanisme ini TERBATAS DAN
TERPREDIKSI terlepas dari rezim pasar (selalu antara `K*N/2` dan `K*N`,
lihat perhitungan order-of-magnitude bagian 3). Ini juga bukan pola baru di
codebase — `top_k` sudah jadi parameter existing di `screen_and_rank`/
`build_portfolio` (Fase 5b) untuk alasan serupa (bound ukuran hasil,
terlepas dari berapa banyak kandidat lolos filter kualitatif).

**Aturan union**: edge AAPL-MSFT terbentuk kalau **SALAH SATU** dari AAPL
atau MSFT punya yang lain di top-5 korelasinya sendiri (bukan harus
KEDUANYA saling menominasi) — beda dari kasus asimetri di desain lama
(bagian 3), di sini AMAN karena nilai korelasi itu sendiri SIMETRIS
(`corr(A,B) == corr(B,A)` selalu, matriks korelasi diagonal-simetris secara
matematis) — yang bisa BEDA cuma RANKING-nya (B mungkin ranking #2 di top-5
AAPL, tapi AAPL cuma ranking #40 di daftar korelasi MSFT kalau MSFT punya
banyak peer lain yang lebih berkorelasi). Union di sini bukan soal
"under-claim vs over-claim" seperti co_mentioned_with di desain lama — murni
soal "pasangan ini cukup penting bagi SALAH SATU pihak untuk dicatat",
dan angkanya sendiri (korelasi) tetap sama presisi di kedua arah, tidak ada
risiko halusinasi arah karena tidak ada LLM yang menafsirkan.

Ranking top-K menggunakan **|correlation_value| (nilai absolut), BUKAN nilai
mentah** — korelasi negatif kuat (mis. -0.75) dianggap SAMA PENTINGNYA dengan
korelasi positif kuat (+0.75) untuk tujuan menemukan hubungan yang secara
statistik signifikan, terlepas arahnya. `correlation_value` tetap DISIMPAN
dengan tanda aslinya (bisa negatif) di field edge (bagian 5.1) — hanya
proses RANKING top-K yang memakai nilai absolut, bukan nilai yang disimpan.

Order-of-magnitude untuk universe ~500 ticker: lihat bagian 3.

### 2.2 Fundamental — Top-K=5 per ticker (konsisten dengan 2.1)

**Keputusan**: top-K=5 per ticker berdasarkan `similarity_score` tertinggi
(bagian 1.2), union sama seperti korelasi, TUNDUK pada syarat minimum 3
metrik overlap (bagian 1.2) — pasangan yang gagal syarat minimum tidak
pernah masuk kandidat top-K sama sekali (bukan masuk lalu dibuang belakangan).

K=5 dipilih SAMA dengan korelasi (bukan angka lain) supaya kedua mekanisme
top-K konsisten dan mudah dinalar bersama — tidak ada alasan kuat kedua jenis
edge ini butuh K berbeda, jadi tidak diberi K berbeda tanpa alasan.

### 2.3 Sektor — WAJIB dibatasi, dipilih: sub-industry SEBAGAI grouping key + cap top-K=5 DI DALAM grup (kombinasi opsi a+c)

**Keputusan konkret**: edge sektor HANYA dibangun ANTAR ticker dalam
`industry` (sub-industry, field Fase 2a) yang SAMA PERSIS (opsi a) — bukan
`gics_sector` besar (Technology/Financials/dst, yang bisa berisi 70+ nama).
DI DALAM satu grup sub-industry, tiap ticker dibatasi ke top-K=5 PEER dalam
grup itu, di-ranking pakai **angka korelasi yang SAMA yang sudah dihitung di
bagian 2.1** (reuse, tidak dihitung ulang) — ini opsi (c) yang diterapkan DI
DALAM subset opsi (a), bukan salah satu dipilih murni sendirian.

Kenapa kombinasi, bukan opsi (a) SENDIRIAN: sub-industry saja TIDAK cukup
membatasi — beberapa sub-industry S&P 500 (mis. "Software - Infrastructure",
"Semiconductors", "Biotechnology", "Regional Banks") masih bisa berisi 30-70+
nama, yang tanpa cap tambahan tetap meledak ke ribuan edge PER grup (lihat
skenario (c) bagian 9 — 70 ticker tanpa cap = 2.415 edge dari SATU grup
saja). Kenapa REUSE korelasi sebagai kriteria ranking (bukan hitung metrik
baru): efisien (tidak ada komputasi tambahan, angkanya sudah ada dari bagian
2.1) DAN semantically masuk akal — "peer paling relevan dalam sub-industry
yang sama" secara wajar diartikan sebagai "yang pergerakan harganya paling
mirip di antara sesama sub-industry", bukan kriteria arbitrer baru.

Field `shared_sector`/`shared_sub_industry` (bagian 4) akan SELALU `True`
keduanya untuk edge yang terbentuk lewat mekanisme ini (sub-industry sama
→ otomatis sektor besar juga sama, sub-industry adalah subset sektor) — kedua
field tetap disimpan (bukan cuma satu) untuk keterbacaan mandiri (pembaca
edge tidak perlu tahu algoritma bounding-nya untuk mengerti kedua fakta itu),
dan supaya skema tidak perlu berubah kalau suatu saat aturan bounding
direlaksasi (mis. mengizinkan edge sektor besar-tanpa-sub-industry-sama untuk
grup sub-industry yang sangat kecil — di luar scope sekarang).

Ranking top-K dalam grup sub-industry (bagian ini) mewarisi aturan yang sama
dari bagian 2.1: berdasarkan **|correlation_value|, bukan nilai mentah** —
dua ticker dalam sub-industry yang sama tapi bergerak berlawanan arah
(korelasi negatif kuat) tetap dianggap peer yang relevan untuk edge
`same_sector`.

Order-of-magnitude: lihat bagian 3 dan skenario (c) bagian 9.

---

## 3. Order-of-Magnitude — Universe ~500 Ticker (S&P 500 Penuh)

**Titik pembanding (WAJIB dihitung, bukan diasumsikan aman)**: pairwise
penuh TANPA pembatasan apa pun untuk 500 ticker × 3 jenis edge =
`C(500,2) × 3 = 124.750 × 3 = 374.250` edge teoretis maksimum kalau semua
pasangan dan semua jenis relasi disimpan tanpa filter — ini angka yang HARUS
dihindari, referensi skala "meledak".

| Jenis edge | Mekanisme bound | Estimasi rentang (union, K=5) | Catatan |
|---|---|---|---|
| `price_correlated` | Top-5 per ticker, union, 500 ticker | `500*5/2` (kalau semua mutual) s.d. `500*5` (kalau tidak ada overlap) = **1.250 – 2.500** | Realistis di tengah rentang ini (korelasi antar large-cap sama sektor sering mutual top-5) |
| `fundamentally_similar` | Top-5 per ticker di antara pasangan yang lolos minimum-3-overlap, union | **~1.000 – 2.000** | Sedikit lebih rendah dari batas atas 2.500 karena syarat minimum overlap mengeliminasi sebagian pasangan sebelum masuk kandidat top-K |
| `same_sector` | Sub-industry sama + top-5 dalam grup | **~1.500 – 2.500** | S&P 500 punya kira-kira 150-170 GICS sub-industry; mayoritas grup KECIL (rata-rata ~3 ticker/grup, kontribusi penuh-pairwise yang sudah kecil dengan sendirinya), cap top-5 baru benar-benar "bekerja" di segelintir grup besar (Software, Semiconductor, Bank, Biotech, REIT, dst) — lihat skenario (c) |
| **TOTAL (3 jenis, sebelum dedup pasangan lintas-jenis)** | — | **~3.750 – 7.000** | ~1-2% dari 374.250 (titik pembanding tanpa filter) — pengurangan >98% dari skala "meledak" |

Catatan penting: total ini BUKAN jumlah pasangan unik — satu pasangan (mis.
AAPL-MSFT) BISA berkontribusi sampai 3 edge terpisah (satu per jenis relasi,
lihat bagian 4 keputusan "TIDAK digabung"), jadi angka total di atas adalah
jumlah EDGE, bukan jumlah PASANGAN ticker yang terhubung.

---

## 4. Menggabungkan 3 Jenis Edge untuk 1 Pasangan Ticker

**Keputusan: TIDAK digabung — SATU pasangan ticker yang lolos ketiga kriteria
menghasilkan 3 edge TERPISAH**, masing-masing dengan `relation_type` sendiri
— bukan 1 edge gabungan dengan 3 flag.

Alasan:
- Konsisten dengan preseden desain lama (`competitor_of` DAN `partner_of`
  disimpan terpisah kalau keduanya berlaku) — tiap `relation_type` adalah
  KLAIM BERBEDA, mencampurnya ke satu edge memaksa field jadi opsional
  tergantung kriteria mana yang cocok (struct "sparse", konsumen harus cek
  field mana yang terisi) — bertentangan dengan pola codebase yang selalu
  membuat entitas/edge dengan field yang PASTI terisi untuk tipenya (lihat
  `_extract_fundamental_metric_entities` yang SKIP metrik null, bukan
  menyimpannya sebagai null di field campuran).
- Tiap jenis edge punya field wajib yang BEDA BENTUK (`correlation_value`
  float tunggal vs `metrics_compared` list vs `shared_sector` bool) — edge
  gabungan berarti attributes-nya jadi union dari ketiga skema, mayoritas
  `None` untuk edge manapun yang cuma lolos 1-2 dari 3 kriteria.
- Konsumen (Fase 11b) bisa query "kenapa AAPL-MSFT terhubung" dan dapat 3
  sinyal independen berlabel jelas, masing-masing bisa diberi bobot/makna
  simulasi berbeda (korelasi harga → "bergerak bersamaan", sektor → "bersaing
  di ruang pasar sama") — bukan satu angka gabungan yang mengaburkan mana
  sinyal mana.

**Enum `relation_type` (closed set, 3 nilai) — SEMUA SIMETRIS eksplisit**:

```python
# ILUSTRASI — mirror gaya _EDGE_SPEC (entity_edge_builder.py) /
# _NEWS_EDGE_SPEC (desain lama, superseded)
_METRIC_EDGE_SPEC: Dict[str, Dict[str, Any]] = {
    "price_correlated":     {"symmetric": True, "noun": "price correlation"},
    "fundamentally_similar": {"symmetric": True, "noun": "fundamental similarity"},
    "same_sector":           {"symmetric": True, "noun": "sub-industry peer"},
}
```

**Ditegaskan eksplisit (supaya tidak ada kebingungan kunci-merge seperti
revisi desain lama)**: KETIGA `relation_type` di sini SELALU
`symmetric=True` — tidak ada satupun yang punya arah "A mempengaruhi B" vs
sebaliknya. Korelasi harga adalah angka simetris secara matematis
(`corr(A,B)==corr(B,A)`), kemiripan fundamental adalah jarak simetris
(`distance(A,B)==distance(B,A)`), dan "sektor sama" jelas tidak punya arah.
Konsekuensinya: **seluruh mekanisme kunci-merge arah-sadar +
`conflicting_direction`/`conflicting_edge_ref` dari revisi desain lama
(bagian 2.5 dokumen SUPERSEDED) TIDAK RELEVAN DAN TIDAK DIPAKAI di sini** —
tidak ada kasus "dua sumber melaporkan arah berlawanan" karena tidak ada
sumber sama sekali yang bisa "melaporkan" secara salah (semua angka
dihitung langsung dari data, bukan diklaim oleh LLM yang bisa keliru
membaca arah).

**Tidak ada logika merge lintas-sumber sama sekali** (beda signifikan dari
desain lama): setiap `(pasangan ticker, relation_type)` punya **PALING
BANYAK SATU edge by construction** — dihitung LANGSUNG dari satu angka
(korelasi/similarity_score/boolean sektor), bukan diakumulasi dari beberapa
observasi artikel terpisah. Tidak ada pertanyaan "bagaimana menggabungkan N
sumber jadi 1 confidence" (probabilistic-OR dkk di desain lama) karena hanya
ada SATU sumber: komputasi itu sendiri.

---

## 5. Field Wajib per Edge (Versi Metrik)

Tidak ada `evidence`/kutipan teks (tidak ada sumber teks sama sekali) — field
pengganti untuk traceability adalah ANGKA/PARAMETER yang menghasilkan edge
itu, supaya "kenapa edge ini ada" tetap bisa diverifikasi ulang (re-run
komputasi yang sama, deterministik — beda dari LLM yang non-deterministik).

### 5.1 `price_correlated`

| Field | Wajib/Opsional | Arti |
|---|---|---|
| `correlation_value` | **Wajib** (float, -1..1) | Pearson correlation harian, dari `returns.corr()` (bagian 1.1). |
| `correlation_period` | **Wajib** (dict: `start`, `end`, `lookback`, `n_observations`) | Rentang tanggal & jumlah observasi PERSIS yang dipakai — bisa direproduksi ulang. |
| `as_of_date` | **Wajib** | `as_of_date` run tempat edge ini dibangun. |
| `rank_a` / `rank_b` | **Opsional** (int, minimal SATU terisi) | Ranking pasangan ini di top-5 korelasi milik masing-masing ticker (1=tertinggi). Salah satu boleh `null` kalau union hanya dinominasikan satu sisi (bagian 2.1). |

### 5.2 `fundamentally_similar`

| Field | Wajib/Opsional | Arti |
|---|---|---|
| `similarity_score` | **Wajib** (float, 0..1] | `1 / (1 + rms_distance)` (bagian 1.2). |
| `metrics_compared` | **Wajib** (list[str], >= 3 elemen) | Metrik overlap AKTUAL yang dipakai untuk PASANGAN ini spesifik (bisa beda per pasangan — bagian 7). |
| `n_metrics_compared` | **Wajib** (int) | `len(metrics_compared)`, kemudahan baca. |
| `as_of_date` | **Wajib** | — |
| `rank_a` / `rank_b` | **Opsional**, sama pola dengan 5.1 | — |

### 5.3 `same_sector`

| Field | Wajib/Opsional | Arti |
|---|---|---|
| `shared_sector` | **Wajib** (bool) | Selalu `True` untuk edge yang terbentuk lewat mekanisme bagian 2.3 (lihat catatan bagian 2.3). |
| `shared_sub_industry` | **Wajib** (bool) | Selalu `True`, sama alasan. |
| `sector` | **Wajib** (str) | Nilai `gics_sector` aktual. |
| `sub_industry` | **Wajib** (str) | Nilai `industry` aktual (Fase 2a). |
| `correlation_value` | **Wajib** (float) | REUSE angka dari bagian 2.1/5.1 — inilah kriteria yang dipakai memilih top-5 dalam grup, jadi wajib ditampilkan supaya tidak menyembunyikan dasar pemilihannya. |
| `as_of_date` | **Wajib** | — |
| `rank_a` / `rank_b` | **Opsional** | Ranking DALAM grup sub-industry (namespace BEDA dari `rank_a`/`rank_b` di 5.1 yang ranking-nya lintas SELURUH universe — jangan tertukar). |

Semua tiga jenis juga membawa `symmetric: True` (dari `_METRIC_EDGE_SPEC`,
bagian 4) di attributes-nya, untuk keseragaman bentuk dengan konvensi edge
Company-to-Company versi lama — bukan karena dibutuhkan untuk logika
merge/kunci apa pun di sini (tidak ada, bagian 4).

```python
# ILUSTRASI — dataclass per jenis edge, sebelum dirakit jadi bentuk
# outgoing/incoming (konvensi Fase 2c, dicontohkan penuh di skenario (a)
# bagian 9). Bukan implementasi.

@dataclass
class CorrelationEdgeAttrs:
    relation_type: str = "price_correlated"
    symmetric: bool = True
    correlation_value: float = 0.0
    correlation_period: Dict[str, Any] = field(default_factory=dict)  # {start, end, lookback, n_observations}
    as_of_date: str = ""
    rank_a: Optional[int] = None
    rank_b: Optional[int] = None


@dataclass
class FundamentalSimilarityEdgeAttrs:
    relation_type: str = "fundamentally_similar"
    symmetric: bool = True
    similarity_score: float = 0.0
    metrics_compared: List[str] = field(default_factory=list)
    n_metrics_compared: int = 0
    as_of_date: str = ""
    rank_a: Optional[int] = None
    rank_b: Optional[int] = None


@dataclass
class SectorEdgeAttrs:
    relation_type: str = "same_sector"
    symmetric: bool = True
    shared_sector: bool = True
    shared_sub_industry: bool = True
    sector: str = ""
    sub_industry: str = ""
    correlation_value: float = 0.0
    as_of_date: str = ""
    rank_a: Optional[int] = None
    rank_b: Optional[int] = None
```

---

## 6. Integrasi dengan Fase 2 — MELENGKAPI (sama pola, dipersingkat)

**Keputusan sama seperti desain lama, alasan sama**: bintang struktural
Fase 2 (Company → Sector/ValuationMetric/FundamentalMetric/TechnicalSignal/
ShariaScreen, murni deterministik) tetap tidak disentuh. Struktur graph
gabungan multi-ticker (inspirasi `build_universe_seed` dari desain lama —
pola strukturalnya SAMA: `build_seed_from_ticker` per ticker tidak diubah →
concat entities jadi satu `FilteredEntities` → tambahkan edge
Company-to-Company sebagai lapisan tambahan → append ke `related_edges`
Company node yang SUDAH punya has_sector/has_metric/has_signal/has_screen
dari Fase 2c, tidak reset) **tetap diperlukan di sini untuk alasan yang
sama persis**: korelasi/similarity/sektor sama-sama beroperasi ANTAR hasil
`build_seed_from_ticker` yang terpisah per ticker, butuh kedua ujung Company
berada di satu graph yang sama supaya `target_node_uuid`/`source_node_uuid`
valid. Detail langkah-per-langkah tidak diulang di sini — lihat bagian 4
dokumen lama untuk pola strukturalnya; yang berubah HANYA isi langkah
"bangun edge Company-to-Company"-nya (sekarang murni komputasi metrik,
bagian 1-5 di atas, bukan panggilan LLM).

---

## 7. Menangani Ticker dengan Data Tidak Lengkap

**Prinsip sama dengan `articles=[]` di desain lama**: ticker TETAP di graph
(bintang Fase 2 penuh, tidak berubah), cuma mungkin tidak menyumbang/menerima
SATU JENIS edge tertentu — tidak pernah di-exclude total dari Fase 9.

Konkretnya per jenis edge:

- **`price_correlated`**: kalau ticker sudah di-drop dari `build_returns_matrix`
  (riwayat harga kurang — mekanisme existing `portfolio_optimizer.py`, bukan
  baru), ticker itu otomatis tidak muncul di matriks korelasi sama sekali →
  nol edge `price_correlated`, dicatat lewat `dropped` list yang SUDAH ada
  di `build_returns_matrix`/`optimize_portfolio` (reuse, tidak perlu mekanisme
  logging baru).
- **`fundamentally_similar`**: dihitung PER-PASANGAN, bukan per-ticker (bagian
  1.2) — kalau ticker X hanya punya sebagian metrik terisi (mis. `roe` dan
  `debt_to_equity` bernilai `None` — field yang sudah di-skip di layer Fase 1
  kalau tidak reliable, lihat `market_data.py`), X **tetap ikut** perbandingan
  dengan ticker LAIN yang overlap metriknya cukup (>= 3, bagian 1.2) — hanya
  metrik yang X punya yang dipakai untuk pasangan itu (`metrics_compared`
  per-pasangan, bisa beda-beda). X di-skip HANYA untuk pasangan spesifik yang
  overlap metriknya < 3, dan kalau X punya < 3 metrik non-null TOTAL, X
  secara struktural tidak akan pernah lolos minimum overlap dengan siapa pun
  → nol edge `fundamentally_similar` untuk X, tapi X TETAP berpartisipasi
  normal di `price_correlated` dan `same_sector` (independen sepenuhnya dari
  kelengkapan data fundamental).
- **`same_sector`**: kalau `fundamental["sector"]`/`["industry"]` kosong untuk
  ticker X (persis kondisi yang sudah bikin `_extract_sector_entity` men-skip
  entity Sector di Fase 2a) — X otomatis tidak masuk grup sub-industry manapun
  → nol edge `same_sector` untuk X, tanpa cabang kode tambahan (X murni tidak
  pernah muncul di pengelompokan `industry`, sama seperti dia tidak pernah
  muncul sebagai entity Sector di graph Fase 2).

Tidak ada satu pun dari tiga kondisi di atas yang mempengaruhi partisipasi X
di dua jenis edge LAINNYA — ketiganya independen, X bisa kaya di satu dimensi
(mis. harga berkorelasi tinggi dengan banyak peer) dan kosong di dimensi lain
(fundamental tak lengkap) tanpa saling menjatuhkan.

---

## 8. Kapan Edge Dihitung Ulang — Fresh Setiap Run, TANPA Cache

**Keputusan: dihitung FRESH setiap kali `build_universe_seed` dipanggil untuk
`(as_of_date, tickers)` tertentu — TIDAK di-cache.** Kontras eksplisit dengan
requirement caching WAJIB di desain lama (bagian 3.4 dokumen SUPERSEDED,
untuk hasil ekstraksi LLM).

Alasan, dipertimbangkan dua sisi:

1. **Kenapa desain lama BUTUH cache**: hasil ekstraksi LLM itu (a) MAHAL
   (biaya API per panggilan) dan (b) NON-DETERMINISTIK (LLM yang sama bisa
   menjawab beda untuk input identik) — cache di situ bukan cuma optimasi
   biaya, tapi PRASYARAT reproducibility (artikel yang sama harus selalu
   hasilkan edge yang sama lintas run).
2. **Kenapa Fase 9 (baru) TIDAK butuh itu sama sekali**: tidak ada LLM →
   tidak ada non-determinisme yang perlu dipin lewat cache (re-run komputasi
   apa pun akan menghasilkan angka IDENTIK dari input yang sama, deterministik
   by construction — numpy/pandas murni). Dan secara biaya: perhitungan
   korelasi Pearson di matriks 500×500 return harian (`returns.corr()`) dan
   jarak RMS di matriks 500×9 metrik fundamental keduanya operasi
   vectorized BLAS/numpy berskala **sub-detik** untuk 500 ticker — jauh di
   bawah waktu yang sudah dihabiskan untuk FETCH harga/fundamental yang
   mendasarinya (yang mana SUDAH di-cache oleh Fase 1, `cache.py`, tidak
   berubah di sini). Menambah lapisan cache di atas komputasi yang sudah
   murah hanya menambah kompleksitas invalidasi (harus tahu kapan universe
   ticker berubah, kapan `as_of_date` beda) untuk penghematan yang hampir
   tidak terasa.
3. Perubahan universe ticker (satu ticker masuk/keluar) ATAU `as_of_date`
   BENAR-BENAR mengubah jawaban yang benar (z-score dihitung ulang atas
   populasi baru, korelasi dihitung ulang atas window baru) — beda dari
   artikel Fase 8 yang isinya immutable terlepas dari kapan diproses. Fresh
   compute di sini bukan cuma "lebih murah", tapi juga LEBIH BENAR secara
   default (tidak ada risiko cache basi menyembunyikan bahwa universe run
   berubah).

Cache Fase 1 di bawahnya (`get_price_data`/`get_fundamental_data`, TTL/
snapshot policy existing, TIDAK diubah) tetap jadi lapisan yang menyerap
biaya jaringan — Fase 9 murni menambahkan komputasi lokal murah di atasnya.

---

## 9. Skenario Konkret

### (a) 2 ticker lolos ketiga kriteria sekaligus

Setup (ILUSTRATIF — nilai fundamental/korelasi diasumsikan untuk skenario
ini, bukan diklaim sebagai angka real-time): `as_of_date="2026-08-20"`,
universe run mencakup MSFT dan ORCL (Oracle) — diasumsikan keduanya punya
`fundamental["industry"] == "Software - Infrastructure"` (sub-industry sama,
konsisten dengan keduanya sama-sama enterprise/infrastructure software).

Langkah manual:
1. **Korelasi (bagian 1.1/2.1)**: `returns.corr()` atas window 1y →
   `corr(MSFT, ORCL) = 0.68`. MSFT me-ranking ORCL di posisi #3 dari top-5
   korelasinya sendiri; ORCL me-ranking MSFT di posisi #1 dari top-5-nya.
   Union → edge `price_correlated` terbentuk, `rank_a` (dari sisi MSFT) = 3,
   `rank_b` (dari sisi ORCL) = 1.
2. **Fundamental (bagian 1.2/2.2)**: kedua ticker punya 9/9 metrik terisi →
   overlap = 9 (>= 3, lolos). Setelah z-score (termasuk log10 market_cap)
   dan winsorize, `rms_distance(MSFT, ORCL) = 0.52` →
   `similarity_score = 1/(1+0.52) = 0.658`. ORCL masuk top-5 fundamental
   MSFT di posisi #2; MSFT masuk top-5 ORCL di posisi #1. Edge
   `fundamentally_similar` terbentuk, `metrics_compared` = seluruh 9 nama
   metrik (union kasus penuh, beda dari skenario (b) di bawah).
3. **Sektor (bagian 1.3/2.3)**: `industry` keduanya sama persis
   ("Software - Infrastructure") → masuk grup sub-industry yang sama. Dalam
   grup itu (asumsikan grup berisi 18 ticker), ORCL adalah peer
   berkorelasi-harga #1 bagi MSFT (dan sebaliknya, memakai angka korelasi
   YANG SAMA dari langkah 1) → keduanya masuk top-5 satu sama lain dalam
   grup → edge `same_sector` terbentuk.

Hasil akhir — **3 edge TERPISAH** untuk pasangan MSFT-ORCL (bagian 4):

```python
# di MSFT::company.related_edges (outgoing, x3 — satu per relation_type)
[
  {
    "direction": "outgoing", "edge_name": "price_correlated",
    "fact": "MSFT and ORCL show a price_correlated relationship "
            "(Pearson r=0.68 over the 1y window ending 2026-08-20).",
    "target_node_uuid": "ORCL::company",
    "attributes": {
        "relation_type": "price_correlated", "symmetric": True,
        "correlation_value": 0.68,
        "correlation_period": {"start": "2025-08-21", "end": "2026-08-20",
                                "lookback": "1y", "n_observations": 251},
        "as_of_date": "2026-08-20", "rank_a": 3, "rank_b": 1,
    },
  },
  {
    "direction": "outgoing", "edge_name": "fundamentally_similar",
    "fact": "MSFT and ORCL show a fundamentally_similar relationship "
            "(similarity_score=0.658 over 9 compared metrics).",
    "target_node_uuid": "ORCL::company",
    "attributes": {
        "relation_type": "fundamentally_similar", "symmetric": True,
        "similarity_score": 0.658,
        "metrics_compared": ["pe_ratio", "pb_ratio", "market_cap",
                              "revenue_growth_yoy", "profit_margin", "roe",
                              "debt_to_equity", "eps_growth", "dividend_yield"],
        "n_metrics_compared": 9,
        "as_of_date": "2026-08-20", "rank_a": 2, "rank_b": 1,
    },
  },
  {
    "direction": "outgoing", "edge_name": "same_sector",
    "fact": "MSFT and ORCL share the Software - Infrastructure sub-industry "
            "as of 2026-08-20 (ranked #1 mutually by price correlation "
            "among their 18 sub-industry peers).",
    "target_node_uuid": "ORCL::company",
    "attributes": {
        "relation_type": "same_sector", "symmetric": True,
        "shared_sector": True, "shared_sub_industry": True,
        "sector": "Technology", "sub_industry": "Software - Infrastructure",
        "correlation_value": 0.68,
        "as_of_date": "2026-08-20", "rank_a": 1, "rank_b": 1,
    },
  },
]
# di ORCL::company.related_edges: 3 edge incoming, attributes IDENTIK persis
# ke masing-masing pasangannya di atas (konvensi Fase 2c, direction dibalik,
# target_node_uuid diganti source_node_uuid="MSFT::company").
```

### (b) 1 ticker dengan sebagian data fundamental `None`

Setup: ticker `XYZ` di universe run yang sama. `get_fundamental_data("XYZ",
...)` mengembalikan `pe_ratio`, `pb_ratio`, `market_cap`, `profit_margin`,
`dividend_yield` terisi, tapi `revenue_growth_yoy`, `roe`, `debt_to_equity`,
`eps_growth` = `None` (field yang di-skip Fase 1 karena tidak reliable untuk
ticker ini) → **6 dari 9 metrik terisi** untuk XYZ.

Langkah manual:
1. **Pasangan XYZ – ABC** (ABC ticker lain dengan 9/9 metrik terisi):
   overlap = irisan metrik yang XYZ punya (6) DENGAN yang ABC punya (9) = 6
   metrik (`pe_ratio`, `pb_ratio`, `market_cap`, `profit_margin`,
   `dividend_yield`, dan sisanya dari 6 milik XYZ semua ada di 9 milik ABC).
   `6 >= 3` → **lolos**, RMS distance dihitung HANYA atas 6 metrik itu, BUKAN
   9 — `metrics_compared` untuk pasangan spesifik ini berisi tepat 6 nama,
   BEDA dari skenario (a) yang berisi 9 nama untuk pasangan lain.
2. **Pasangan XYZ – DEF** (DEF ticker lain yang JUGA data parsial, cuma
   `pe_ratio` dan `market_cap` terisi): overlap = irisan 6 metrik XYZ DENGAN
   2 metrik DEF = paling banyak 2 (`pe_ratio`, `market_cap`). `2 < 3` →
   **pasangan ini DI-SKIP sepenuhnya** dari `fundamentally_similar` — tidak
   ada edge XYZ-DEF jenis ini, dicatat di log dengan alasan eksplisit
   `"overlap=2 metrics < minimum 3"` (bukan silently absent tanpa jejak).
3. XYZ **tetap berpartisipasi penuh** di `price_correlated` (independen dari
   data fundamental) dan berpotensi `same_sector` (kalau `sector`/`industry`
   terisi — field kategori terpisah dari 9 metrik numerik di atas, TIDAK
   ikut skip meski beberapa metrik numerik-nya `None`).

Hasil: XYZ MUNCUL di beberapa edge `fundamentally_similar` (dengan
`metrics_compared` yang lebih pendek dari rekan-rekan datanya-lengkap), TIDAK
MUNCUL di sebagian lain (yang overlap-nya kurang), dan sama sekali TIDAK
kehilangan partisipasi di dua jenis edge lainnya — persis prinsip
graceful-degradation bagian 7.

### (c) Sektor besar (70+ ticker) — pembatasan mencegah ledakan kombinatorial

Setup: sub-industry hipotetis "Application Software" berisi **72 ticker**
dalam universe run S&P 500 (`industry` sama persis untuk ke-72 nya).

**Tanpa pembatasan** (full pairwise dalam grup): `C(72,2) = 72*71/2 =
2.556` edge `same_sector` HANYA dari grup ini saja — angka yang sudah
dihitung sebelumnya sebagai contoh "meledak" (dekat dengan estimasi ~2.500
yang direferensikan task).

**Dengan pembatasan bagian 2.3** (top-K=5 dalam grup, ranking by
`correlation_value` yang di-reuse dari bagian 2.1): tiap ticker dalam grup
menominasikan maksimum 5 peer → `72 * 5 = 360` nominasi mentah. Setelah
dedup union (edge terbentuk kalau SALAH SATU sisi menominasikan): rentang
realistis **~200 – 360 edge** (batas bawah kalau nominasi banyak yang
mutual/tumpang-tindih, batas atas kalau nyaris tidak ada overlap).

**Reduksi**: dari 2.556 (tanpa cap) ke ~200-360 (dengan cap) = **pengurangan
~86% – 92%** untuk grup sebesar ini — konkret, bukan janji "pasti terkendali".
Grup sub-industry yang LEBIH KECIL (mayoritas grup S&P 500, rata-rata ~3
ticker/grup per estimasi bagian 3) tidak pernah menyentuh cap sama sekali
(`C(3,2)=3` sudah di bawah `K=5`), jadi mekanisme top-K di situ efektifnya
adalah no-op yang aman — tidak perlu cabang kode khusus "kalau grup kecil,
skip cap" (top-K dengan `K > group_size-1` otomatis mengambil semua anggota
grup, tidak perlu logika tambahan).

### (d) Korelasi negatif kuat tetap masuk top-K

Setup: ticker `OILCO` (produsen minyak) dan `SOLARCO` (energi surya) dalam
universe run yang sama, `correlation_value = -0.72` (bergerak berlawanan
arah, pola historis wajar untuk dua sub-sektor energi yang bersaing).

Langkah manual: `|−0.72| = 0.72`, dibandingkan dengan korelasi POSITIF lain
yang mungkin dimiliki OILCO (mis. `+0.45` dengan produsen minyak lain).
Karena ranking pakai nilai absolut (bagian 2.1), `0.72 > 0.45` → SOLARCO
masuk top-5 OILCO meski nilainya negatif, mengalahkan peer berkorelasi-
positif yang lebih lemah. Edge `price_correlated` terbentuk dengan
`correlation_value = -0.72` (tanda asli DIPERTAHANKAN di field, bagian 5.1)
— konsumen downstream (Fase 11b) bisa melihat dari tandanya bahwa ini
hubungan berlawanan arah, bukan searah, meski proses seleksinya sama-sama
"top-K berdasarkan kekuatan hubungan".

---

## Ringkasan Keputusan

| Poin | Keputusan |
|---|---|
| 1a. Korelasi — sumber | REUSE `build_returns_matrix` (`portfolio_optimizer.py`), lookback `"1y"` (sama default). Pearson `.corr()` DIHITUNG BARU di Fase 9 — beda dari Ledoit-Wolf shrinkage covariance yang dipakai optimizer (tujuan beda). |
| 1b. Fundamental — sumber & normalisasi | 9 metrik numerik dari `get_fundamental_data` (`market_data.py`). `market_cap` di-log10 dulu (WAJIB), z-score per metrik atas populasi universe run (skip null), winsorize [-3,+3]. Jarak = RMS distance (dibagi jumlah metrik overlap, bukan raw sum) → `similarity_score = 1/(1+rms_distance)`. |
| 1c. Sektor — sumber | Reuse entity `Sector` Fase 2a (`gics_sector`, `industry`) — tidak ada data baru. |
| 2a. Threshold korelasi | Top-K=5 per ticker, union (ranking by \|correlation_value\|, tanda asli dipertahankan di field). Threshold tetap DITOLAK (regime-dependent, tidak terprediksi). |
| 2b. Threshold fundamental | Top-K=5 per ticker (konsisten dgn korelasi), union, tunduk minimum 3 metrik overlap. |
| 2c. Threshold sektor | Sub-industry (`industry`) sebagai grouping key (opsi a) + cap top-K=5 dalam grup, ranking by korelasi reuse (opsi c) — kombinasi, bukan salah satu sendirian (ranking by \|correlation_value\|, tanda asli dipertahankan di field). |
| 3. Order-of-magnitude (500 ticker) | ~3.750–7.000 edge total (3 jenis) vs 374.250 titik pembanding tanpa filter — reduksi >98%, dihitung eksplisit bukan diasumsikan. |
| 4. Gabung 3 jenis edge | TIDAK digabung — 3 edge terpisah per pasangan yang lolos >1 kriteria. Enum `relation_type` 3 nilai, SEMUA `symmetric=True` — tidak ada mekanisme kunci-merge arah-sadar/`conflicting_direction` (tidak relevan, tidak ada arah). Tidak ada logika merge lintas-sumber (1 sumber = 1 edge by construction). |
| 5. Field wajib | Per jenis: `correlation_value`+`correlation_period`; `similarity_score`+`metrics_compared`; `shared_sector`+`shared_sub_industry`+`sector`+`sub_industry`+`correlation_value`. Semua + `as_of_date`, `rank_a`/`rank_b` opsional. Traceability via angka/parameter reproducible, bukan kutipan teks. |
| 6. Integrasi Fase 2 | MELENGKAPI, sama pola `build_universe_seed` (append ke `related_edges`, tidak reset bintang Fase 2c). |
| 7. Data fundamental parsial | Ticker tetap di graph (bintang Fase 2 penuh). Per-PASANGAN (bukan per-ticker): overlap metrik dihitung ulang tiap pasangan, pasangan dgn overlap < 3 di-skip individual, tidak mempengaruhi partisipasi di `price_correlated`/`same_sector`. |
| 8. Caching | TIDAK di-cache — dihitung fresh tiap run. Beda dari desain lama (LLM) karena di sini tidak ada non-determinisme untuk dipin dan komputasinya sub-detik (cache Fase 1 di bawahnya sudah menyerap biaya network). |

**JANGAN mulai implementasi (Fase 9b baru) — dokumen ini menunggu review.**
