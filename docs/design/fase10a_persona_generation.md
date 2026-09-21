# Fase 10a — Desain Generate 8 Persona (gaya MiroFish original)

Status: **desain, belum implementasi** (Fase 10b menunggu review dokumen ini).

Konteks: Fase 9 (Cluster Graph) sudah berbasis metrik tanpa LLM. Fase 10 adalah
langkah **terpisah total**: LLM bebas menciptakan **PERSIS 8** persona investor
(kepribadian, filosofi, bias), non-deterministik, dengan berita Fase 8 sebagai
*grounding kreatif* (bukan fakta yang diverifikasi).

**Tidak disentuh:** jalur Fase 3 (`oasis_profile_generator.py`: 6 persona tetap,
stance dihitung dari angka pasti, `generate_investor_persona`, dst.) tetap ada
untuk portofolio konsensus lama. Fase 10 adalah jalur paralel, bukan pengganti.

Yang dibaca (tidak diubah):
- `backend/app/services/oasis_profile_generator.py` — pola `create_chat_completion`
  + `response_format={"type": "json_object"}` + loop `max_attempts=3` dengan
  temperature turun per attempt (`0.7 - attempt*0.1` di jalur entitas umum;
  `0.6 - attempt*0.2` di jalur investor) + penanganan `finish_reason == 'length'`
  (`_fix_truncated_json`) + fallback `_try_fix_json` + `time.sleep(1*(attempt+1))`
  saat exception.
- `backend/app/data_layer/news_data.py` — `get_news_data(ticker, as_of_date)`;
  field artikel: `article_id` (dedup key), `headline`, `summary` (nullable),
  `publisher`, `url`, `published_at`, `related_ticker`. Kontrak `success`,
  `out_of_range`, `caveat`, `articles` (`[]` bila di luar jangkauan ~360 hari).
- `backend/app/data_layer/universe.py` — `screen_universe(sectors, market_cap_tiers,
  as_of_date)` mengembalikan list entry `{ticker, company_name, gics_sector,
  market_cap, market_cap_tier}` **sudah terurut market cap menurun**. Inilah cara
  daftar ticker sebuah run didapat.

---

## 1. Skema persona

Struct ilustratif (bukan file `.py`, tidak dijalankan):

```python
@dataclass
class InvestorPersona:
    # --- wajib, diisi LLM ---
    name: str                    # unik dalam 1 run, dikarang LLM (bukan nama tokoh nyata,
                                 # bukan nama 5 archetype Fase 3)
    tagline: str                 # 1 kalimat, <= 140 karakter
    investment_philosophy: str   # 2-4 kalimat: cara memandang pasar, apa yang dicari
    philosophy_label: str        # label bebas 1-4 kata ("momentum chaser", "contrarian
                                 # skeptic", "yield-hungry pragmatist"). TIDAK enum tertutup.
    biases: List[str]            # 2-4 bias/blind spot khas persona ini
    personality: str             # 2-4 kalimat: watak, cara berpikir
    communication_style: str     # 2-4 kalimat: gaya menulis/posting (dipakai OASIS nanti)
    edge_vs_others: str          # 1 kalimat: apa yang membedakan dari 7 persona lain
    # --- opsional ---
    short_bio: Optional[str]     # 1-3 kalimat latar belakang fiktif
```

Yang **sengaja tidak ada**: `stance`, `score`, `verdict`, `confidence`, atau
angka apa pun per ticker. Berbeda dari Fase 3 (stance dihitung dari angka pasti),
di Fase 10 stance terhadap ticker SPESIFIK baru muncul saat persona benar-benar
berdebat di OASIS (Fase 11b). Ia bukan bagian identitas dasar.

Filosofi tidak dibatasi daftar tertutup: boleh mirip value/growth, boleh sama
sekali baru. `philosophy_label` sengaja string bebas dan hanya dipakai untuk
soft-check keberagaman (bagian 5), bukan untuk logika lain.

`edge_vs_others` ada supaya LLM dipaksa menyatakan diferensiasinya secara
eksplisit saat menulis (teknik anti-collapse, bagian 3), dan supaya reviewer bisa
mengaudit keberagaman dengan cepat.

Wadah output LLM: `{"personas": [ {…}, … 8 item … ]}` — objek di puncak karena
`response_format={"type": "json_object"}` tidak menjamin array di puncak.

---

## 2. Strategi sampling berita — DIPILIH: (a) top-N market cap, dengan dedup ala (b)

**Aturan konkret:**

1. Ambil daftar universe run dari `screen_universe(...)` (sudah terurut market cap
   menurun).
2. Pilih **maksimum 20 ticker** dengan **batas 5 ticker per `gics_sector`**: lewati
   entry berikutnya dari sektor yang sudah penuh 5, lanjut ke entry berikutnya
   sampai 20 terkumpul atau universe habis. (Tanpa batas sektor, top-20 S&P 500 by
   market cap didominasi Information Technology/Communication Services dan persona
   jadi bias ke tema teknologi.)
3. Untuk tiap ticker panggil `get_news_data(ticker, as_of_date)` dan ambil
   **3 artikel terbaru** (urut `published_at` menurun).
4. **Dedup by `article_id`** lintas seluruh sample (artikel yang sama bisa muncul
   di lebih dari satu ticker). Artikel duplikat dibuang, TIDAK diganti artikel
   ke-4 (batas sederhana, konsisten).
5. **Total maksimum 60 artikel** (20 × 3). Tiap artikel masuk prompt sebagai:
   `[published_at tanggal] ticker | publisher | headline | summary dipotong 200
   karakter` (summary nullable → dilewati). Perkiraan ~60-80 token/artikel →
   **≈ 4-5 ribu token** total, jauh di bawah context window.

**Alasan memilih (a) dan bukan (b)/(c):**
- **Deterministik pada sisi input.** Sampling acak (b) menambah sumber
  non-determinisme kedua. Kita ingin non-determinisme hanya berasal dari LLM
  (keputusan sadar, bagian 4), sehingga sample input untuk `(universe, as_of_date)`
  yang sama identik dan bisa diaudit lewat daftar `article_id`.
- **Cache-friendly & rate-limit-friendly.** Ticker yang sama dipanggil berulang
  di banyak run → hit cache `get_news_data`. Maksimum 20 panggilan Finnhub per run
  jauh di bawah limit 60 request/menit (Fase 8a/8b), tanpa perlu throttling.
- **Relevan untuk grounding.** Persona digenerate sekali per run untuk "dunia"
  itu; berita perusahaan-perusahaan terbesar (dengan penyeimbang sektor)
  mewakili tema makro/sektor yang sedang hangat.
- Dari (b) hanya konsep **dedup by `article_id`** yang dipinjam (mengacu ke
  algoritma Fase 9 versi berita yang SUPERSEDED — referensi desain saja, file
  implementasinya tidak pernah ditulis dan tidak ada yang di-import).

**Kasus tepi (bukan kegagalan):**
- Universe < 20 ticker → pakai semua yang ada.
- `as_of_date` lebih tua dari ~360 hari → `get_news_data` mengembalikan
  `out_of_range=True, articles=[]` untuk semua ticker. Total artikel 0.
- Total artikel setelah dedup **< 10** (termasuk 0) → generate **tanpa blok berita**
  dan tandai `grounding = "none"` di metadata run. Persona tetap dibuat (grounding
  hanya bahan tambahan; identitas persona tidak wajib bergantung pada berita).
  Flag ini dicatat, jangan disembunyikan (prinsip "flag, don't fix silently").
- Satu ticker `success=False` (mis. 429) → ticker itu dilewati, bukan digantikan
  ticker lain; jumlah artikel lebih sedikit, aturan `< 10` di atas tetap berlaku.
- Leakage: karena `as_of_date` diteruskan ke `get_news_data`, artikel setelah
  `as_of_date` sudah dibuang di lapisan itu. Fase 10 tidak menambah logika tanggal
  sendiri, dan sesuai aturan konsistensi `as_of_date` dari audit Fase 8a.

---

## 3. Non-determinisme — kontras dengan Fase 3

Fase 3 memakai temperature **rendah** (0.5-0.7, turun tiap retry) karena LLM hanya
menulis narasi di sekitar angka pasti; variasi tidak diinginkan. Fase 10 butuh
**kebalikannya**.

**Rekomendasi temperature: 1.0 pada attempt pertama, rentang kerja 0.8-1.0.**
- Attempt 1: `1.0`, attempt 2: `0.9`, attempt 3: `0.8` (`1.0 - attempt*0.1`).
  Pola "turun per retry" dipertahankan karena retry dipicu masalah struktural
  (JSON rusak, jumlah salah); menurunkan sedikit membantu kepatuhan format, tapi
  lantainya dijaga di 0.8 supaya tidak kembali ke rezim "clone".
- Alasan tidak di atas 1.0: sebagian provider OpenAI-compatible/Anthropic
  membatasi ke [0,1], dan >1.0 mulai meningkatkan JSON rusak tanpa menambah
  keberagaman berarti. Nilai final di-tune saat 10b, angka ini titik awal.

**Anti-collapse dalam SATU run — DIPILIH: 1 panggilan LLM yang meminta 8 sekaligus.**

| | 1 panggilan × 8 persona | 8 panggilan × 1 persona |
|---|---|---|
| Keberagaman internal | Model melihat ke-8 sekaligus, bisa menyebar posisi secara sadar | Tiap panggilan tidak tahu yang lain → cenderung konvergen ke mode yang sama ("Buffett-ish" berulang) |
| Perbaikan | Bisa dipaksa "saling berbeda" + `edge_vs_others` | Harus menyuntik daftar persona sebelumnya (jadi sekuensial, lambat) |
| Biaya | 1 panggilan (~6-8 ribu token output) | 8 panggilan |
| Risiko | Output panjang → kena `finish_reason=='length'` atau JSON terpotong; kegagalan menggagalkan semua | Kegagalan terisolasi |
| Konsistensi struktur | 1 JSON, mudah divalidasi "tepat 8" | Perlu agregasi |

Dipilih 1 panggilan karena risiko utama Fase 10 adalah **collapse** (8 clone), dan
hanya pendekatan ini yang membuat model membandingkan seluruh set. Risiko
pemotongan output dimitigasi dengan tidak menetapkan `max_tokens` rendah
(mengikuti pola existing: "tidak set max_tokens") + `_fix_truncated_json` + retry.
Terjadi collapse sebagian tetap mungkin, lihat soft-check bagian 5.

**Teknik prompt anti-collapse** (lihat contoh prompt di bawah):
1. Perintah eksplisit "8 persona yang SALING BERBEDA, tidak boleh ada dua dengan
   filosofi/gaya/bias yang bisa dipertukarkan".
2. Paksa sebar pada sumbu: horizon waktu (hari → dekade), sikap risiko, cara
   memakai berita/informasi, gaya komunikasi. Tiap sumbu minimal 3 nilai berbeda
   di antara 8 persona.
3. Wajib min. 2 persona di luar "investor arif konvensional" (mis. skeptis kontrarian,
   pengejar momentum, ...) dan min. 1 persona yang irasional/emosional secara
   sadar (bias kuat).
4. Larang nama tokoh nyata & nama 5 archetype Fase 3.
5. Field `edge_vs_others` memaksa refleksi diferensiasi.

---

## 4. Reproducibility yang HILANG — keterbatasan SADAR

Dua run dengan `as_of_date` dan universe yang sama BISA menghasilkan 8 persona
yang berbeda. **Ini disengaja, bukan bug dan bukan keterbatasan data.** Ini keputusan
desain sadar sesuai instruksi task (replikasi gaya MiroFish original dengan LLM
bebas berkreasi, bukan determinisme Fase 3). Ini kontras dengan prinsip "flag, don't
fix silently" untuk keterbatasan data: di sini tidak ada yang "diperbaiki diam-diam"
karena tidak ada yang rusak. Konsekuensinya, hasil debat (Fase 11b) dua backtest
untuk tanggal yang sama tidak bisa dibandingkan apple-to-apple hanya dari
`as_of_date`. Untuk perbandingan yang adil, persona hasil satu run harus dipakai
ulang, bukan digenerate ulang. Karena itu **usulan untuk 10b (dinilai reviewer)**:
tiap run menyimpan persona + metadata (`run_id`, `generated_at`, `model`,
`temperature_used`, `attempt`, daftar `article_id` sample, `grounding`
`"news"|"none"`). Ini bukan membuat generasi deterministik, hanya membuat satu
hasil dapat dipakai ulang dan diaudit.

---

## 5. Validasi output — proporsional

**Dilakukan (struktural):**
1. JSON ter-parse (pola existing: `json.loads` → gagal → perbaikan JSON;
   `finish_reason=='length'` → `_fix_truncated_json`). Catatan: `_try_fix_json`
   yang ada punya signature spesifik entitas Fase 3 (argumen nama/tipe entitas,
   flag `_fixed`), jadi 10b memakai **pola**-nya (regex/perbaikan newline & kutip,
   tutup kurung terpotong) lewat helper tipis sendiri, bukan memanggilnya apa adanya.
2. Kunci `personas` ada dan berupa list.
3. **Panjang list persis 8.**
4. Tiap item punya semua field wajib (bagian 1) non-kosong dengan tipe benar
   (`biases` list string dengan 2-4 item).
5. **Nama tidak duplikat** dalam 1 run (dibandingkan setelah `strip().casefold()`).

**Soft-check (hanya log warning, tidak memicu retry):** `philosophy_label`
duplikat setelah normalisasi; nama identik dengan salah satu 5 archetype Fase 3.
Ini sinyal collapse untuk reviewer, bukan alasan membuang hasil.

**Sengaja TIDAK dilakukan:** hallucination-guard evidence-quote seperti Fase 9
versi berita. Alasan: di Fase 9 berita, LLM mengklaim fakta ("perusahaan X
bermitra dengan Y") yang bisa diverifikasi benar/salah terhadap teks sumber, dan
klaim salah merusak data downstream. Di Fase 10 output adalah **karakter fiktif**;
kepribadian/filosofi tidak punya nilai kebenaran yang bisa diverifikasi, dan berita
hanya inspirasi. Menuntut kutipan bukti akan mematikan kreativitas yang justru
jadi tujuan. Rigor kita taruh di kontrak struktural (agar Fase 11a bisa
memetakan dengan aman), bukan pada isi.

**Kondisi retry:** parse gagal setelah perbaikan; `personas` bukan list;
jumlah ≠ 8; field wajib hilang/kosong; nama duplikat; exception API
(+ `sleep(1*(attempt+1))`). Maks **3 attempt**, seluruh 8 persona digenerate
ulang (bukan menambal sebagian, agar keberagaman tetap dinilai model atas set utuh).

**Setelah 3 attempt gagal:** TIDAK ada fallback rule-based seperti Fase 3 (fallback
templat deterministik bertentangan dengan tujuan Fase 10). Fungsi mengembalikan
kegagalan eksplisit (`success=False` + `error`), dan pemanggil memutuskan
(batalkan run). Jangan diam-diam menurunkan jumlah persona.

---

## 6. Batas tanggung jawab ke Fase 11a

Output Fase 10 **berhenti di identitas persona** (skema bagian 1). Fase 10 TIDAK
membuat follow-network, activity profile (response latency, engagement frequency),
sentiment baseline, atau influence weight — semua itu dipetakan **Fase 11a** dari
output ini (mis. dari `personality` dan `communication_style`). Kalau saat
implementasi 10b ada godaan menambah field OASIS teknis ke skema, itu scope creep
ke 11a dan harus ditolak.

---

## Contoh prompt LLM (ilustratif, tidak dijalankan)

**System:**

```text
You are a creative director for an investment-debate simulation. You invent
fictional retail and professional investors with distinct minds. You are NOT
building a balanced panel of sensible experts: you are building 8 characters
who would genuinely disagree, argue and irritate each other in a public
social-media discussion about stocks.

Rules:
- Output ONLY a JSON object: {"personas": [ ...exactly 8 objects... ]}
- Every persona must be meaningfully DIFFERENT from every other one. No two may
  share an interchangeable philosophy, communication style or bias set.
- Do not use real people's names, and do not name a persona after a classic
  archetype (Value, Growth, Quant, Macro, Sentiment).
- Investment philosophy is open: it may resemble a known school or be something
  new (e.g. "momentum chaser", "contrarian skeptic"). Do not restrict yourself.
- Do NOT assign stances, ratings or scores on any specific stock. Those come
  later, during the debate.
```

**User:**

```text
Invent exactly 8 investor personas who are mutually different.

Spread the 8 across these axes (each axis must show at least 3 distinct values):
 - time horizon (days ... decades)
 - attitude to risk
 - how they use news and information
 - communication style (terse, rambling, sarcastic, academic, hype-driven, ...)
At least 2 must be unconventional (not a sober fundamental investor), and at
least 1 must be strongly emotional or irrational in a way that is a bias.

Use the news below only as loose inspiration for the market mood. You do not
need to reference it, and you must not treat it as facts to verify.

MARKET NEWS SAMPLE (as of 2025-06-30, 47 articles, deduplicated):
[2025-06-27] NVDA | Reuters | Chip stocks rally as AI capex guidance lifted | ...
[2025-06-27] JPM  | Bloomberg | Banks pass Fed stress test, plan buybacks | ...
[2025-06-26] XOM  | CNBC | Oil slips as demand outlook softens | ...
[2025-06-26] PFE  | MarketWatch | Drugmaker cuts guidance on pricing pressure | ...
... (max 60 lines, summary truncated to 200 chars)

Return JSON:
{"personas": [
  {
    "name": "unique invented name",
    "tagline": "one sentence, <=140 chars",
    "investment_philosophy": "2-4 sentences",
    "philosophy_label": "1-4 free-form words",
    "biases": ["2-4 short items"],
    "personality": "2-4 sentences",
    "communication_style": "2-4 sentences, how they write posts",
    "edge_vs_others": "one sentence: what makes them unlike the other 7",
    "short_bio": "optional, 1-3 sentences"
  }, ...
]}
```

Bila `grounding = "none"`, blok `MARKET NEWS SAMPLE` dihilangkan dan kalimat
"Use the news below…" diganti dengan instruksi bebas tanpa referensi berita.

---

## Skenario A — 8 persona berhasil (ilustrasi keberagaman, bukan output LLM sungguhan)

Sample: universe S&P 500, `as_of_date` dalam jangkauan; 20 ticker (maks 5 per
sektor), 61 kandidat artikel → 47 setelah dedup (≥10, jadi `grounding="news"`).
Berita bertema rally chip AI, lulus stress test bank, minyak melemah, farmasi
cut guidance. Attempt 1, temperature 1.0. JSON valid, 8 item, 8 nama unik →
lolos. Contoh 3-4 dari 8 yang diharapkan beragam:

- **"Marisol Vance"** — *contrarian skeptic*. Menganggap rally chip AI sebagai
  tanda puncak; horizon berbulan-bulan; bias: mencari alasan ragu pada tiap
  konsensus. Gaya: kalimat pendek, sinis.
- **"Dex Okonkwo-Hale"** — *momentum chaser*. Membeli yang naik, jual yang
  patah; horizon hari-minggu; bias: recency dan FOMO. Gaya: hype, huruf kapital
  dan emoji.
- **"Dr. Ilse Brandvold"** — *balance-sheet purist*. Hanya peduli kualitas neraca
  dan arus kas, mengabaikan berita harian; horizon dekade; bias: melewatkan
  perusahaan bagus yang terlihat "mahal". Gaya: akademis, panjang, berhati-hati.
- **"Tante Rina"** — *yield-hungry pragmatist* dengan bias home/familiarity.
  Mengejar dividen stabil dari bank dan energi; bias: overweight nama yang ia
  kenal. Gaya: hangat, cerita anekdot.

Tidak ada yang menyatakan "BUY/SELL" pada ticker tertentu; itu tugas Fase 11b.
Soft-check: label filosofi 8/8 unik, tidak ada nama archetype Fase 3 → tanpa
warning. Hasil beserta metadata (`article_id` sample, `temperature_used=1.0`,
`attempt=1`) disimpan.

## Skenario B — 7 persona atau nama duplikat → retry

**B1 (7 item):** Attempt 1 (temp 1.0): LLM mengembalikan JSON valid tetapi
`len(personas) == 7` (mis. terpotong untuk hemat token). Validasi butir 3 gagal →
tercatat `warning: expected 8 personas, got 7 (attempt 1)` → retry attempt 2 pada
temp 0.9 dengan prompt yang **sama** (seluruh 8 digenerate ulang, tanpa ditambal).
Attempt 2 menghasilkan 8 item unik → lolos. Metadata mencatat `attempt=2`.

**B2 (nama duplikat):** Attempt 1 menghasilkan 8 item tetapi dua item bernama
"Marcus Reid" dan "marcus reid " (sama setelah `strip().casefold()`) → butir 5
gagal → retry (temp 0.9). Bila attempt 2 juga gagal (mis. JSON terpotong,
`finish_reason=='length'` → `_fix_truncated_json`-style repair gagal parse),
attempt 3 (temp 0.8). Bila attempt 3 tetap gagal validasi struktural → kembalikan
`success=False` dengan `error` yang menyebut kegagalan terakhir dan **tidak**
mengisi sebagian atau fallback templat; run dibatalkan oleh pemanggil.

**B3 (bukan retry):** 8 item, 8 nama unik, tapi dua `philosophy_label` sama
("value investor") → hanya soft-check: warning di log, hasil diterima.

---

## Pertanyaan terbuka untuk reviewer

1. Batas 20 ticker / 5 per sektor / 3 artikel / summary 200 karakter —
   sudah sesuai, atau ingin angka lain?
2. Setuju persistence persona per run (bagian 4) dimasukkan ke scope 10b, atau
   dibiarkan sebagai pekerjaan terpisah?
3. Temperature awal 1.0 dengan lantai 0.8 — cocok dengan provider/model yang
   dipakai di `.env`? (Batas atas parameter bergantung provider.)
4. Tanpa fallback rule-based setelah 3 kali gagal — sudah sesuai harapan?
