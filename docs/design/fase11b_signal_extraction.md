# Fase 11b — Desain Ekstraksi Sinyal dari Simulasi OASIS

Status: **desain, belum implementasi** (menunggu review). Tidak ada file di `backend/app/services/`
atau `backend/app/data_layer/` dibuat/diubah untuk menyusun dokumen ini. Semua nama fungsi/skema baru
di bawah bersifat **ilustratif**.

Dasar: `docs/investigations/fase11_e2e_verification.md` (verifikasi E2E sungguhan),
`docs/investigations/fase11a0_oasis_schema.md` (skema `actions.jsonl`/DB), dan data **nyata** dari
`backend/uploads/simulations/sim_e2e_verify_20260922_090624/` (dibaca, TIDAK diubah). Semua angka di
dokumen ini dihitung ulang langsung dari `twitter/actions.jsonl` dan `reddit/actions.jsonl` di direktori
itu — bukan disalin dari laporan sebelumnya tanpa verifikasi.

**Koreksi angka terhadap KONTEKS tugas:** KONTEKS menyebut "46 unik Twitter, 33 unik Reddit setelah
dedup dari 80". Setelah dihitung ulang dari data sungguhan dengan definisi dedup §1 (per `(agent_id,
teks_persis)`, **termasuk** `quote_content` milik `QUOTE_POST` sebagai konten bermakna — yang tidak
dihitung di laporan verifikasi E2E sebelumnya), angka yang benar adalah **42 unik Twitter, 33 unik
Reddit** (dari 68 aksi berkonten Twitter dan 80 aksi berkonten Reddit di round ≥1; lihat §1 dan §8 untuk
rincian). Angka Reddit (33) kebetulan cocok; angka Twitter (42, bukan 46) dikoreksi di sini karena
sekarang menghitung `QUOTE_POST` secara eksplisit.

---

## Ringkasan keputusan (referensi cepat)

| # | Keputusan | Alasan singkat |
|---|---|---|
| 1 | Dedup: **exact byte-match**, key `(agent_id, teks)` — TANPA `action_type` di key, TANPA normalisasi whitespace/case, **lintas SELURUH simulasi** (bukan per-window) | Semua duplikat nyata di data memang byte-identik; window-based akan salah menghitung repetisi verbatim sebagai "minat baru" |
| 2 | Unit ekstraksi = 1 konten unik pasca-dedup → **list** `{ticker, stance, score}` (multi-ticker didukung) | Konten nyata ada yang sebut 2 ticker sekaligus (Morgan: `$GOOGL` & `$AAPL`) |
| 3 | Metode: **(c) 1 panggilan batch per platform**, pola index+validasi seperti enrichment Fase 11a (BUKAN Fase 9 evidence-quote ketat) | 2 panggilan, ≈11,7K token total — <6% biaya OASIS sendiri (~198K token); heuristik terbukti salah baca hedge nyata (Elena, §3) |
| 4 | Agregasi = **riwayat mentah per (persona,ticker)** + `latest_stance`/`latest_score` sebagai kenyamanan, TANPA formula recency-weighted tunggal | Fase 12 adalah LLM yang menimbang sendiri, bukan optimizer — jangan pra-kompresi informasi trend |
| 5 | Follow-network: **diabaikan** (graf lengkap = tanpa varians = tanpa sinyal pembeda). Engagement: sinyal sekunder untuk penulis ASLI, dihitung dari DB (post_id), bukan dari teks `actions.jsonl` | Semua agent 7/7 follower/following di kedua platform — dikonfirmasi ulang di run ini |
| 6 | Output: dict bersarang per persona→ticker, WAJIB bisa ditelusuri balik ke `round`+`agent_id`+`action_type`+teks verbatim | Prinsip traceability yang sama sejak Fase 9, bentuk disesuaikan (bukan evidence-quote ketat) |
| 7 | Ticker yang tidak pernah dibahas: **domain Fase 11b** (dilaporkan sebagai `tickers_silent` di output), bukan didelegasikan ke Fase 12 | Fase 11b satu-satunya komponen yang punya universe HASIL SCREENING + `actions.jsonl` sekaligus |

---

## 1. Algoritma dedup — PERSIS

### Key dedup: `(agent_id, teks_persis)` — lintas semua `action_type`, lintas SELURUH simulasi

**TIDAK** termasuk `action_type` di key. Rasional: dedup ini menjawab pertanyaan "apakah persona ini
mengucapkan opini yang SAMA PERSIS lagi", bukan "apakah aksi platform-nya sama". Dalam data nyata tidak
pernah terjadi teks identik lintas `action_type` berbeda (CREATE_POST vs QUOTE_POST punya field teks
terpisah: `content` vs `quote_content`), jadi secara praktis key ini berperilaku sama dengan
`(agent_id, action_type, teks)` — tapi definisi yang lebih longgar ini benar secara konsep dan tidak
akan pecah kalau suatu saat memang terjadi (mis. persona menyalin ulang post lama sebagai comment).

**Field teks yang dipakai per `action_type`** (lihat §8 untuk bukti struktur nyata):

| `action_type` | Field teks | Alasan |
|---|---|---|
| `CREATE_POST` | `action_args.content` | Opini penuh |
| `CREATE_COMMENT` | `action_args.content` | Opini penuh |
| `QUOTE_POST` | `action_args.quote_content` | Komentar BARU dari agent yang mengutip — **BUKAN** `original_content` (itu milik penulis lain) |
| `REPOST`, `LIKE_POST`, `DISLIKE_POST`, `FOLLOW`, `MUTE`, dll | *(tidak ada)* | Tidak ada teks baru dari agent ini — lihat §2 untuk bagaimana ini tetap dipakai (sebagai engagement, §5), bukan sebagai unit dedup/ekstraksi |

### Normalisasi: **TIDAK ADA** (exact byte match, hanya `strip()` whitespace ujung)

**Posisi yang diambil:** dedup memakai kecocokan **byte-per-byte** (setelah `strip()` whitespace ujung
saja — bukan lowercasing, bukan menghapus tanda baca/emoji/hashtag). Alasan, dirujuk langsung ke data:

1. **Semua duplikat nyata yang ditemukan MEMANG identik byte-per-byte**, bukan mirip-tapi-beda. Contoh
   kasus uji yang diminta KONTEKS — Isaac (`agent_id=7`, Reddit): teks *"AI stocks like $GOOGL are
   indeed the future. Let's capitalize on these opportunities and ride the wave of technological
   advancement. #TechInnovation #InvestSmart"* muncul **7×, byte-identik**, di round **3, 5, 6, 7, 8, 9,
   10**. Tidak ada variasi kapitalisasi/spasi sama sekali antar kemunculan — jadi normalisasi
   case/whitespace tidak dibutuhkan untuk menangkap kasus ini.
2. **Near-duplicate BUKAN target dedup ini, dan menyamakannya akan membuang informasi.** Elena
   (`agent_id=3`, Twitter) menulis 4 varian yang mirip tapi **tidak identik**:
   - *"...it's **time** to reassess. The 2 AI stocks... **might not** be as solid as they seem. #MarketInsight"*
   - *"...it's **important** to reassess. The 2 AI stocks... #MarketInsight #SentimentWhisperer"*
   - *"...it's **crucial** to reassess. The **two** AI stocks... **as they appear**. Let's carefully
     evaluate the fundamentals before making any investment decisions. #MarketInsight #SentimentWhisperer"*

   Ini BUKAN kemalasan model (pola Isaac/repetisi verbatim) — ini variasi leksikal genuin, tanda model
   masih mencoba menulis ulang. Fuzzy-matching akan menyamakannya jadi 1 unit dan **menyembunyikan**
   bahwa Elena tetap menyatakan sikap serupa dengan kalimat baru tiap kali (sinyal yang justru berguna:
   ia BUKAN salah satu agent yang "macet", lihat §8).
3. **Kesederhanaan & audit.** Exact-match tidak butuh threshold similarity yang harus dijustifikasi
   (mis. cosine similarity > 0.9 — angka arbitrer tanpa data pendukung). Exact-match bisa diverifikasi
   manual dalam satu baris kode; fuzzy-matching butuh keputusan tambahan yang tidak ada buktinya
   dibutuhkan oleh data ini.

**Fuzzy/near-dup dedup EKSPLISIT DITOLAK untuk versi ini** — dicatat sebagai kemungkinan pekerjaan
lanjutan bila data run yang lebih besar nanti menunjukkan pola near-duplicate yang signifikan (belum
terbukti dari 1 run verifikasi ini).

### Cakupan dedup: LINTAS SELURUH SIMULASI (bukan per-window waktu)

**Posisi yang diambil:** 1 pasangan `(agent_id, teks)` = **1 kemunculan unik**, terlepas dari seberapa
jauh round-nya terpisah. Kasus uji Isaac (round 3, lalu 5–10) **tidak** dianggap "opini baru yang muncul
lagi di round 5" — itu tetap kalimat yang SAMA PERSIS yang pertama kali diucapkan di round 3. Dedup
per-window (mis. per 3 round) akan menghasilkan Isaac dihitung sebagai **3 kemunculan unik terpisah**
(round 3 sendirian; round 5-7 sebagai 1 window; round 8-10 sebagai window lain) — ini secara faktual
SALAH, karena tidak ada kata baru yang ditulis Isaac di antara round-round itu. Window-based dedup akan
diam-diam mengembalikan bobot berlebih ke repetisi yang justru ingin kita hilangkan (KEPUTUSAN SUDAH
DIAMBIL di KONTEKS: "konten identik dihitung SEKALI, bukan N kali").

**Yang disimpan setelah dedup — konten unik + DAFTAR round (bukan cuma hitungan):**

```
{
  "agent_id": 7, "agent_name": "Isaac 'The Irrational Genius' Illusionist",
  "platform": "reddit", "action_type": "CREATE_COMMENT",
  "text": "AI stocks like $GOOGL are indeed the future. Let's capitalize on these opportunities and ride the wave of technological advancement. #TechInnovation #InvestSmart",
  "rounds": [3, 5, 6, 7, 8, 9, 10],
  "occurrence_count": 7
}
```

**Alasan menyimpan daftar round (bukan cuma hitungan):** (a) **audit** — bisa ditelusuri balik ke baris
persis di `actions.jsonl`; (b) **input untuk §4** — `rounds` dipakai untuk `mentioned_in_rounds` di level
persona-ticker dan untuk membedakan "disebut sekali di awal lalu diam" vs "disebut berulang sepanjang
simulasi" (keduanya informasi berbeda yang hilang kalau cuma disimpan `occurrence_count=7`).

### Dedup TIDAK berlaku untuk round 0

`round 0` di kedua platform berisi (a) 56× `FOLLOW` (seeding graf lengkap, `ManualAction`, bukan opini)
dan (b) 4× `CREATE_POST` initial — **tapi initial post BUKAN opini persona**, isinya headline berita asli
disalin verbatim oleh `_post_content()` (Fase 11a), contoh: *"Warren Buffett Just Stepped Down. These 2
AI Stocks Still Define Berkshire's Portfolio $GOOGL — via Yahoo (2026-09-22)"*. Ini adalah **event seed**,
bukan pendapat agent — jadi **dikeluarkan sepenuhnya** dari unit dedup/ekstraksi opini (§2), meski tetap
relevan sebagai konteks/pemicu diskusi. Semua penghitungan "aksi berkonten" di dokumen ini adalah
**round ≥ 1 saja**, kecuali disebut eksplisit sebaliknya.

---

## 2. Unit ekstraksi sinyal

### Unit = 1 konten unik pasca-dedup → list `{ticker, stance, score}`

Setiap konten unik (hasil §1) yang menyebut ≥1 `$TICKER` diproses **sekali** (bukan sekali per
kemunculan) menjadi **list** entri ticker — BUKAN dipaksa satu ticker per konten, karena data nyata
menunjukkan post multi-ticker itu ada: Morgan (`agent_id=0`, Reddit) menulis *"Let's focus on AI stocks
like $GOOGL and $AAPL. Embrace the irrationality to thrive! #MarketAnomalies #InvestSmart"* → harus
menghasilkan 2 entri (`GOOGL`, `AAPL`), yang LLM boleh (dan di kasus ini kemungkinan besar akan) memberi
stance yang sama (keduanya bullish, karena teksnya tidak membedakan sikap per ticker) — tapi struktur
output harus mendukung nilai berbeda per ticker seandainya teksnya memang membedakan.

### Representasi stance: kategorikal DAN skor kontinu, keduanya

- `stance`: salah satu dari `"bullish" | "bearish" | "neutral" | "mixed"` (4 kategori — `"mixed"`
  ditambahkan karena ada kasus nyata yang secara eksplisit menimbang dua sisi, lihat Elena/Jordan §8,
  bukan cuma bullish/bearish/neutral 3-kategori yang memaksa pembulatan).
- `score`: float `-1.0..1.0` (kontinu).

**Alasan menyimpan keduanya** (biaya tambahan minimal — 1 field ekstra dalam panggilan LLM yang sama):
kategori untuk keterbacaan manusia/logika ambang batas sederhana di Fase 12 nanti; skor kontinu supaya
agregasi §4 (rata-rata, urutan kekuatan sinyal) tidak harus memetakan kategori ke angka secara implisit
di kode yang berbeda dari yang menghasilkannya.

### Aksi non-tekstual (`LIKE_POST`, `REPOST`, `DISLIKE_POST`, `MUTE`)

**Posisi yang diambil: DIKELUARKAN dari ekstraksi stance milik agent yang melakukannya.** `REPOST` dan
`LIKE_POST` tidak membawa teks baru dari agent tersebut (dikonfirmasi dari struktur nyata `action_args`
— lihat §8: `REPOST` hanya punya `new_post_id`, `original_content`, `original_author_name`, TIDAK ada
komentar tambahan). Memaksa ekstraksi stance dari sebuah "like" tanpa teks berarti **mengarang** opini
yang tidak pernah ditulis agent itu — bertentangan dengan prinsip "tanpa fallback/tanpa mengarang" yang
konsisten dipegang sejak Fase 10 (`persona_generator.py`) dan Fase 11a (`persona_oasis_adapter.py`,
`get_or_generate_enrichment`: gagal → `success=False`, bukan isi diam-diam).

Sebagai gantinya, `LIKE_POST`/`REPOST`/`QUOTE_POST` (bagian relasi-ke-post-asli, TERPISAH dari
`quote_content` milik `QUOTE_POST` sendiri yang tetap diekstrak sebagai opini quoter — lihat §1) dipakai
sebagai **sinyal engagement untuk PENULIS ASLI**, bukan opini baru untuk agent yang bereaksi. Detail dan
alasan lengkap di §5.

---

## 3. Metode ekstraksi — dipilih: **(c) 1 panggilan batch per platform**

### Perbandingan 3 opsi terhadap data nyata (bukan hipotetis)

**(b) Heuristik/leksikon — DITOLAK, dengan bukti kegagalan konkret dari data nyata:**
Elena menulis *"...The 2 AI stocks defining Berkshire's portfolio **might not be as solid** as they
seem."* — leksikon kata kunci sederhana (bullish: "solid", "strong", "hold"; bearish: "crash", "sell",
"overvalued") akan mencocokkan kata **"solid"** dan salah membaca ini sebagai **bullish**, padahal
klausanya adalah **"might NOT be as solid"** — sebuah *hedge*/keraguan, bukan keyakinan positif. Ini
BUKAN kasus buatan — ini kalimat sungguhan yang ditulis 4× (dengan variasi kecil) oleh Elena di Twitter.
Kasus serupa: Jordan (Reddit) — *"AI stocks like $GOOGL still hold strong. **Don't rush** into anything
**without doing** your own research."* — leksikon akan menangkap "hold strong" sebagai sinyal bullish
kuat, padahal framing sebenarnya berhati-hati/menahan diri. Negasi dan *hedging* seperti ini butuh
pemahaman konteks kalimat, bukan pencocokan kata kunci — risiko yang sudah diperingatkan KONTEKS dan
sekarang **terbukti nyata** di data.

**(a) 1 panggilan LLM per konten unik — DITOLAK, bukan karena salah, tapi kalah efisien dari (c):**
75 panggilan (42 Twitter + 33 Reddit) berarti 75 kali overhead system-prompt (yang mendominasi biaya
karena konten rata-rata sangat pendek, ~41-47 token) DAN 75 kali round-trip latency ke endpoint RunPod
(setiap panggilan menunggu giliran, tidak seperti OASIS yang mem-paralelkan lewat `env.step()` batch).
Untuk konten sependek ini, overhead per-panggilan (bukan isi) yang mendominasi biaya — lihat estimasi
di bawah.

**(c) 1 panggilan batch per PLATFORM (2 panggilan total: Twitter, Reddit) — DIPILIH.**

**Pola rigor yang diikuti: pola `get_or_generate_enrichment` Fase 11a (`persona_oasis_adapter.py`),
BUKAN pola Fase 9.** Alasan eksplisit kenapa Fase 9 (ketat, evidence-quote wajib) tidak proporsional di
sini: Fase 9 memvalidasi **klaim faktual tentang dunia nyata** dari sumber eksternal (berita) — hallucination
di situ berarti mengarang FAKTA yang bisa diverifikasi salah. Di sini, LLM diminta menginterpretasi
**teks yang SUDAH ada, tertulis penuh di `actions.jsonl`**, ditulis oleh persona FIKTIF — tidak ada
"fakta dunia nyata" yang bisa di-hallucinate; risiko yang ada adalah **kesalahan interpretasi** (salah
baca sarkasme/hedge, atau di batch: atribusi silang), bukan pengarangan fakta. Maka pola yang dipakai
adalah pola enrichment (index eksplisit + echo-back validasi + retry TANPA fallback diam-diam bila
gagal), bukan pola evidence-quote Fase 9.

**Mitigasi risiko atribusi-silang (risiko yang SAMA yang sudah ditimbang di Fase 9, ditangani dengan pola
BERBEDA yang sudah terbukti di Fase 11a):** setiap item diberi `index` eksplisit dalam prompt (0..N-1),
LLM WAJIB meng-echo `index` (dan potongan pendek teks aslinya, mis. 20 karakter pertama, sebagai
verifikasi tambahan) di tiap objek output — PERSIS pola yang sudah divalidasi bekerja di
`_validate_enrichment` (`persona_oasis_adapter.py`: validasi `index` posisi + `name` echo-back per item,
gagal → retry, gagal 3× → `success=False` tanpa fallback). Bila jumlah item output ≠ jumlah input, atau
`index`/potongan-teks tidak cocok posisi → **retry seluruh batch** (bukan terima sebagian) — sama seperti
enrichment, bukan "terima yang valid, buang yang tidak" (itu akan diam-diam kehilangan sinyal tanpa
warning eksplisit).

### Estimasi biaya token — DIHITUNG dari data nyata (bukan tebakan), dibandingkan OASIS

| | Twitter | Reddit | Total |
|---|---|---|---|
| Konten unik pasca-dedup (§1, §8) | 42 | 33 | **75** |
| Total karakter konten unik | 6.889 | 6.192 | 13.081 |
| Rata-rata karakter/item | 164 | 188 | — |
| Rata-rata token/item (÷4) | ≈41 | ≈47 | — |

**Asumsi eksplisit untuk estimasi (mengikuti pola panggilan enrichment yang sudah diukur presisi di
`docs/investigations/fase11_e2e_verification.md` §7 — bukan tebakan buta):** overhead system+instruksi
per panggilan batch ≈500 token (mirip `_ENRICHMENT_SYSTEM_PROMPT`+`_ENRICHMENT_USER_RULES` yang terukur
≈300-400 token untuk 8 item; dilebarkan sedikit untuk instruksi multi-ticker+stance+score); tiap item
dalam prompt butuh ≈25-30 token tambahan untuk framing (`[index] agent: ... platform: ... text: ...`) di
atas token konten mentahnya; tiap item output (index echo + 1-2 entri ticker + stance + score + potongan
verifikasi) ≈65-70 token.

- **Twitter (1 panggilan, 42 item):** prompt ≈ 500 + 42×(41+28) ≈ 500 + 2.898 ≈ **3.398 token** |
  completion ≈ 42×68 ≈ **2.856 token** | **≈6.254 token**
- **Reddit (1 panggilan, 33 item):** prompt ≈ 500 + 33×(47+28) ≈ 500 + 2.475 ≈ **2.975 token** |
  completion ≈ 33×68 ≈ **2.244 token** | **≈5.219 token**
- **TOTAL ekstraksi sinyal: ≈11.473 token, 2 panggilan LLM.**

**Dibandingkan biaya OASIS yang MENGHASILKAN data ini** (`fase11_e2e_verification.md` §7: ≈198.400 token
estimasi untuk 155 panggilan keputusan agent): **ekstraksi sinyal ≈5,8% dari biaya OASIS itu sendiri** —
argumen langsung untuk kenapa memilih heuristik gratis (opsi b) untuk "menghemat" tidak masuk akal:
penghematan ≈11K token itu kecil dibanding ≈198K token yang SUDAH dibayar untuk memproduksi datanya, dan
opsi (b) terbukti (§3 atas) bisa salah baca hedge/negasi — risiko kualitas sinyal yang jauh lebih mahal
konsekuensinya (Fase 12 mengambil keputusan portofolio dari sinyal yang salah) dibanding ≈11K token.

Untuk perbandingan opsi (a) yang ditolak: 75 panggilan terpisah, overhead ≈250 token/panggilan (system
prompt individual, lebih kecil dari batch tapi diulang 75×) + ≈44 token konten rata-rata + ≈65 token
completion ≈ 75×359 ≈ **26.900 token** DAN 75 round-trip terpisah (bukan 2) — opsi (c) lebih murah
**dan** lebih cepat.

---

## 4. Agregasi jadi trajectory per (persona, ticker)

### Posisi yang diambil: simpan RIWAYAT MENTAH, jangan pra-kompresi jadi 1 angka tunggal

**Alasan utama, dirujuk ke arsitektur Fase 12 sendiri:** KONTEKS eksplisit menyatakan Fase 12 adalah
**LLM yang menentukan bobot, BUKAN optimizer matematis**. Sebuah optimizer numerik butuh 1 angka bersih
per (persona, ticker) sebagai input (mis. rata-rata tertimbang recency). Sebuah **LLM** justru lebih baik
diberi **riwayat/tren mentah** (list kejadian terurut waktu) dan membiarkan LLM Fase 12 sendiri yang
menilai apakah tren itu "konsisten sejak awal" vs "baru berubah di akhir" — meringkas riwayat jadi 1
angka SEBELUM sampai ke LLM Fase 12 berarti membuang informasi yang justru paling cocok dibaca LLM
(bukan matematika). Karena itu Fase 11b **tidak** menghitung formula recency-weighted average sebagai
output utama — hanya menyediakan `latest_stance`/`latest_score` sebagai **kenyamanan** (bukan satu-
satunya sumber kebenaran), di samping `history` penuh.

### Skema agregasi per (persona, ticker)

```
{
  "latest_stance": "bearish",      # dari unique-content DENGAN round TERAKHIR (max(rounds) tertinggi
                                    # di antara semua entri history milik ticker ini)
  "latest_score": -0.6,
  "evidence_count": 2,             # jumlah KONTEN UNIK (pasca-dedup) berbeda yang menyebut ticker ini —
                                    # BUKAN jumlah kemunculan mentah. Isaac->GOOGL = 1 (1 konten unik,
                                    # walau muncul 7x); Elena->GOOGL (4 varian near-dup, TIDAK dedup
                                    # sesuai §1) = 4.
  "mentioned_in_rounds": [1,3,4,5,6,7,8,9,10],   # union semua round dari semua entri history, urut naik
  "history": [ /* satu entri per konten unik, urut round pertama kemunculan naik — lihat §6 skema penuh */ ]
}
```

### Bobot: `evidence_count` sebagai sinyal keyakinan — TAPI diserahkan ke Fase 12, bukan dihitung di sini

`evidence_count` (banyak konten unik BERBEDA yang menyebut ticker itu) memang informatif: persona yang
menyebut ticker di 3 konten unik dengan kata-kata berbeda (mis. Elena, 4 varian near-dup soal GOOGL)
menunjukkan **keterlibatan lebih tinggi** dibanding persona yang menyebutnya sekali lalu diam. Tapi Fase
11b **tidak mengonversi ini jadi bobot numerik tunggal** (mis. "kalikan score dengan log(evidence_count)")
— itu adalah keputusan pembobotan yang secara eksplisit domain Fase 12 (LLM yang menimbang). Fase 11b
hanya melaporkan `evidence_count` sebagai fakta, tidak menafsirkannya.

### Persona yang TIDAK PERNAH menyebut suatu ticker

**Posisi yang diambil: KEY TIDAK ADA di dict ticker milik persona itu** — BUKAN entri dengan
`stance: "neutral"`/`score: 0.0`. Alasan: `"neutral"` yang di-generate LLM (dari konten yang memang
membahas ticker itu dengan nada netral) dan "tidak pernah membahas sama sekali" adalah **dua fakta yang
sangat berbeda** secara epistemik — yang pertama adalah SINYAL (persona ini punya pendapat, dan
pendapatnya adalah "netral"), yang kedua adalah KETIADAAN SINYAL (kita tidak tahu pendapat persona ini
sama sekali). Menyamakan keduanya sebagai `score: 0.0` akan membuat Fase 12 salah menyimpulkan "8/8
persona netral terhadap ticker X" padahal yang benar adalah "0/8 persona pernah membahas ticker X" —
perbedaan yang krusial untuk keputusan portofolio. Lihat §7 untuk bagaimana ini dilaporkan di level
ticker (bukan per-persona).

---

## 5. Follow-network dan engagement

### Follow-network: **diabaikan** — dikonfirmasi ulang dari data run ini, bukan hanya preflight

Follow-network Fase 11a adalah **graf lengkap** (semua follow semua). Dikonfirmasi ULANG dari
`fase11_e2e_verification.md` §4 (dibaca dari tabel `follow` sungguhan run ini, bukan hanya klaim desain):
**SEMUA 8 agent punya PERSIS 7 follower dan 7 following di KEDUA platform** — tanpa kecuali, tanpa
varians. Struktur ini secara matematis **tidak membawa informasi pembeda** — tidak ada agent yang lebih
"sentral" atau "berpengaruh secara struktural" dibanding agent lain, karena semua node identik secara
topologi. Menganalisis follow-network lebih jauh di Fase 11b (mis. skor sentralitas graf) akan
menghasilkan angka yang **sama untuk semua 8 persona** — komputasi tanpa nilai tambah. **Keputusan:
follow-network TIDAK dianalisis lebih lanjut di Fase 11b**, konsisten dengan catatan investigasi 11a-0
bahwa perannya murni mekanisme OASIS (memastikan feed berjalan), bukan sinyal.

### Engagement (LIKE/REPOST/QUOTE): sinyal sekunder untuk PENULIS ASLI, dari DB — bukan dari teks `actions.jsonl`

**Posisi:** engagement dipakai sebagai **penguat confidence** untuk konten yang di-like/repost/quote,
dikreditkan ke **PENULIS ASLI** (bukan ke agent yang like/repost — lihat §2, itu bukan opini baru
mereka).

**Kenapa dari DB, bukan `actions.jsonl`:** struktur nyata `REPOST` (§8) hanya menyimpan
`original_content`+`original_author_name` (teks, untuk keterbacaan log), **BUKAN** `post_id` numerik dari
post asli — jadi menautkan repost ke konten unik hasil dedup §1 lewat `actions.jsonl` saja berarti
mencocokkan STRING (rapuh: rusak kalau ada 2 post beda tapi kebetulan teksnya identik dari agent yang
berbeda — jarang tapi mungkin). Tabel `post`/`comment` di `twitter_simulation.db`/`reddit_simulation.db`
punya `original_post_id`/kolom relasi numerik — **join berbasis ID** di level implementasi (Fase 11c)
lebih andal daripada mencocokkan teks di level `actions.jsonl`. Fase 11b hanya mensyaratkan engagement
count TERSEDIA sebagai field opsional; DARI MANA angka itu didapat adalah keputusan implementasi 11c.

### Twitter vs Reddit — DIPERLAKUKAN BEDA, dengan bukti dari run ini sendiri (bukan cuma investigasi 11a-0)

Investigasi 11a-0 mencatat tabel `follow` Reddit tidak dibaca recsys-nya. Run verifikasi E2E menambah
bukti konkret yang LEBIH KUAT: **di run nyata ini, Reddit menghasilkan NOL aksi `LIKE_POST`/
`DISLIKE_POST`** (`action_type_distribution` Reddit = `{FOLLOW:56, CREATE_POST:13, CREATE_COMMENT:71}` —
tidak ada `LIKE_POST` sama sekali). Artinya **data engagement Reddit di run ini kosong total** — bukan
cuma "kurang berpengaruh", betul-betul tidak ada untuk dianalisis. Twitter sebaliknya punya `LIKE_POST:3,
REPOST:3, QUOTE_POST:22` — `QUOTE_POST` dominan, dan `QUOTE_POST` **bukan murni engagement struktural**
karena punya `quote_content` sendiri (opini baru, sudah diekstrak sebagai konten first-class di §1-2).
**Kesimpulan:** desain Fase 11b tidak boleh MENGASUMSIKAN engagement selalu tersedia di kedua platform —
field engagement bersifat **opsional/bisa kosong per platform**, dan Reddit kemungkinan besar akan sering
kosong berdasarkan pola run ini (perlu diverifikasi lagi di run-run berikutnya sebelum dianggap pola
tetap, bukan cuma 1 titik data).

---

## 6. Output schema — kontrak untuk Fase 12

```jsonc
{
  "simulation_id": "sim_e2e_verify_20260922_090624",
  "persona_run_id": "af0dbfbdbe77439b8a26dc4ae3c23581",
  "universe": {
    "screened_tickers": ["XOM", "CVX", ... /* 44 ticker hasil screen_universe */],
    "tickers_discussed": ["GOOGL", "META", "NFLX", "CVX", "AAPL"],   // union round 0 + round>=1, kedua platform
    "tickers_silent": ["XOM", "COP", ... /* screened_tickers - tickers_discussed */]
  },
  "personas": {
    "Isaac 'The Irrational Genius' Illusionist": {
      "agent_id": 7,
      "tickers": {
        "GOOGL": {
          "latest_stance": "bullish",
          "latest_score": 0.7,
          "evidence_count": 1,
          "mentioned_in_rounds": [3, 5, 6, 7, 8, 9, 10],
          "history": [
            {
              "platform": "reddit",
              "action_type": "CREATE_COMMENT",
              "text": "AI stocks like $GOOGL are indeed the future. Let's capitalize on these opportunities and ride the wave of technological advancement. #TechInnovation #InvestSmart",
              "rounds": [3, 5, 6, 7, 8, 9, 10],
              "occurrence_count": 7,
              "stance": "bullish",
              "score": 0.7,
              "engagement": {"likes": 0, "reposts_or_shares": 0, "quotes": 0},
              "source_ref": {"agent_id": 7, "round_first_seen": 3, "action_type": "CREATE_COMMENT", "platform": "reddit"}
            }
          ]
        }
      }
      // ticker yang TIDAK PERNAH disebut Isaac -> TIDAK ADA key-nya di sini (lihat §4)
    }
    // ... 7 persona lain
  },
  "extraction_meta": {
    "method": "llm_batch_per_platform",
    "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
    "unique_contents_twitter": 42,
    "unique_contents_reddit": 33,
    "llm_calls": 2
  }
}
```

### Field wajib vs opsional

| Field | Wajib? | Catatan |
|---|---|---|
| `tickers.<T>.latest_stance`, `.latest_score` | **Wajib** | Turunan langsung dari `history`, selalu bisa dihitung bila `history` tidak kosong |
| `tickers.<T>.evidence_count`, `.mentioned_in_rounds` | **Wajib** | Sama |
| `tickers.<T>.history[].text`, `.rounds`, `.stance`, `.score` | **Wajib** | Inti traceability — lihat di bawah |
| `tickers.<T>.history[].engagement` | **Opsional** | Bisa `null`/kosong (lihat §5 — Reddit sering kosong); Fase 12 harus toleran field ini absen/nol |
| `universe.tickers_silent` | **Wajib** | Lihat §7 |
| `history[].source_ref.action_type/agent_id/round_first_seen/platform` | **Wajib** | Traceability minimum |
| `source_ref.post_id`/`comment_id` (DB numerik) | **Opsional, direkomendasikan** | Lebih presisi dari `text`+`round` untuk audit lewat DB langsung; ketersediaannya tergantung implementasi 11c |

### Traceability — prinsip yang sama sejak Fase 9, bentuk yang sesuai

Fase 9 mewajibkan evidence-quote yang bisa diverifikasi terhadap sumber berita eksternal (klaim faktual).
Di sini, "sumber" adalah `actions.jsonl`/DB milik simulasi sendiri — jadi bentuk traceability yang
proporsional adalah **referensi balik** (`round` + `agent_id` + `action_type` + `platform` + teks verbatim
persis seperti tersimpan), bukan kutipan-dengan-verifikasi seperti Fase 9. `text` di setiap entri
`history` SELALU teks PERSIS (byte-per-byte) dari `action_args.content`/`quote_content` asli — siapa pun
bisa `grep` teks itu langsung di `actions.jsonl` untuk audit manual, tanpa butuh akses DB. Ini memenuhi
maksud yang sama (auditability) dengan biaya implementasi jauh lebih rendah dari Fase 9.

---

## 7. Ticker yang tidak pernah dibahas sama sekali — domain Fase 11b

**Posisi yang diambil: Fase 11b MELAPORKAN (`universe.tickers_silent` di skema §6), bukan didiamkan dan
diserahkan ke Fase 12 untuk dihitung ulang.**

**Alasan:** Fase 11b adalah satu-satunya titik di pipeline yang punya AKSES LANGSUNG ke dua hal
sekaligus — (a) daftar lengkap ticker hasil `screen_universe` (via `personas_result`/`simulation_config.
json`, sudah tersimpan sejak Fase 10/11a) dan (b) seluruh `actions.jsonl` kedua platform. Menghitung
`tickers_silent = screened_tickers − tickers_discussed` adalah operasi trivial DI SINI karena kedua input
sudah ada. Mendelegasikan ini ke Fase 12 berarti Fase 12 harus (i) menerima ulang daftar universe penuh
DAN (ii) melakukan diff sendiri terhadap 8 dict persona — pekerjaan yang sama, dilakukan ulang di tempat
yang lebih jauh dari sumber data, berisiko error (mis. lupa satu ticker yang cuma disebut di round 0 tapi
tidak di round ≥1). Prinsip: **hitung sekali, di tempat yang paling dekat dengan data mentah.**

### Bukti konkret dari run nyata — 3 kasus, bukan hipotetis

1. **CVX — disebut di round 0 (seed dari headline asli), lalu TOTAL DIAM setelahnya di KEDUA platform.**
   Initial post Jordan (round 0): *"Chevron (CVX) Stock Drops Despite Market Gains... $CVX — via Yahoo
   (2026-09-21)"*. Menghitung ulang mention `$CVX` di seluruh konten unik round ≥1 (kedua platform):
   **0 kemunculan.** Dari 4 ticker yang di-seed di round 0 (GOOGL, META, NFLX, CVX), **CVX adalah satu-
   satunya yang benar-benar mati total** setelah round 0 — bahkan ticker yang SUDAH disuntikkan lewat
   `initial_posts` tidak menjamin akan didiskusikan. Ini bukti langsung bahwa mekanisme seed 4-post
   (desain Fase 11a §8) tidak cukup untuk menjamin cakupan diskusi — relevan untuk siapa pun yang nanti
   mendesain seed event yang lebih kaya (di luar cakupan Fase 11b, dicatat sebagai temuan).

2. **AAPL — TIDAK PERNAH di-seed di round 0, tapi muncul organik di round ≥1 (Reddit, 3× di antara konten
   unik).** Morgan menulis *"Let's focus on AI stocks like $GOOGL and $AAPL..."* — ticker ini masuk
   diskusi murni dari asosiasi bebas model ("AI stocks" → menyebut Apple juga), bukan dari event yang
   disuntikkan. Menunjukkan ticker BISA masuk diskusi tanpa di-seed, tapi ini **tidak bisa diandalkan**
   (hanya 1 dari 44 ticker di universe yang muncul organik seperti ini di run ini) — universe screening
   44 ticker jelas tidak bisa mengandalkan "semoga muncul organik" sebagai strategi cakupan.

3. **BRK.A — disebut (Rachel, Reddit round 1: *"...Implications for Berkshire Hathaway's Future
   $BRK.A — via Bloomberg (2026-09-22)"*) TAPI TIDAK ADA di `screened_tickers` hasil `screen_universe`
   run ini sama sekali** (sektor Energy + Communication Services tidak mencakup Financials, tempat
   Berkshire Hathaway berada). Model bahkan MENGARANG format sitasi `"via Bloomberg (tanggal)"` yang meniru
   gaya `initial_posts` asli — padahal ini BUKAN artikel nyata yang di-fetch dari Finnhub. **Ini kasus
   ekstra di luar 7 poin KONTEKS, tapi krusial untuk implementasi 11c:** ekstraksi sinyal (§3) HARUS
   memvalidasi setiap ticker yang disebut terhadap `screened_tickers`, dan ticker DI LUAR universe (seperti
   `BRK.A` di sini) harus **ditandai/dibuang**, bukan diteruskan diam-diam ke Fase 12 — Fase 12 tidak
   akan punya data fundamental/harga untuk ticker di luar universe yang sudah di-screening, jadi
   menerima sinyal untuknya cuma akan membingungkan, bukan membantu. Ditambahkan sebagai aturan wajib
   di §6 (implisit di `universe.screened_tickers` — validasi keanggotaan adalah langkah wajib sebelum
   sebuah ticker boleh masuk ke `personas.*.tickers`).

---

## 8. Contoh konkret end-to-end (data NYATA, `sim_e2e_verify_20260922_090624`)

### 8a. Dedup — kasus Isaac (7 kemunculan) sebelum → sesudah

**Sebelum dedup** (7 baris terpisah di `reddit/actions.jsonl`, semua `agent_id=7`,
`action_type=CREATE_COMMENT`):

```
round 3  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 5  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 6  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 7  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 8  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 9  Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
round 10 Isaac  CREATE_COMMENT  "AI stocks like $GOOGL are indeed the future. Let's capitalize..."
```

**Sesudah dedup (§1):** 1 grup `(agent_id=7, teks)`, `rounds=[3,5,6,7,8,9,10]`, `occurrence_count=7`.

**Sesudah ekstraksi (§2-3, hasil ILUSTRATIF — bukan hasil panggilan LLM sungguhan, karena dokumen ini
desain bukan implementasi):** `GOOGL → {"stance": "bullish", "score": 0.7}` — kategori bullish jelas
dari "capitalize on these opportunities" + "ride the wave", tidak ada hedge/negasi di kalimat ini
(kontras dengan kasus Elena §3), jadi confidence tinggi masuk akal (skor ilustratif 0.7, bukan 1.0
karena tetap teks pendek generik tanpa penjelasan mendalam).

**Hasil akhir di trajectory Isaac→GOOGL:** `evidence_count=1` (HANYA 1 konten unik, meski muncul 7×) —
ini secara eksplisit **BUKAN** `evidence_count=7`, sesuai keputusan KONTEKS "dihitung SEKALI, bukan N
kali". `mentioned_in_rounds=[3,5,6,7,8,9,10]` tetap disimpan (union dari `rounds`) untuk
menunjukkan Isaac konsisten bullish GOOGL sejak round 3 sampai akhir simulasi — sinyal DURASI (bertahan
7 dari 10 round) yang tetap bermakna untuk Fase 12, walau `evidence_count`-nya cuma 1.

### 8b. Persona yang membahas ticker di luar 4 initial post — ADA, tapi keduanya di luar universe (bukan "ticker lain di dalam universe"), temuan tambahan untuk §7

Diperiksa langsung terhadap `screened_tickers` run ini (44 ticker, sektor Energy + Communication Services
saja): **tidak satu pun dari 40 ticker lain di universe yang tidak di-seed pernah dibahas** oleh persona
manapun di round ≥1. Perluasan pembahasan yang memang terjadi organik justru menyimpang ke DUA ticker
**di luar universe run ini sepenuhnya**:

- **AAPL** (Morgan, Reddit) — ticker populer ("AI stocks like $GOOGL and $AAPL"), tapi AAPL ada di
  sektor Information Technology, **bukan** salah satu dari 2 sektor yang di-screening run ini.
- **BRK.A** (Rachel, Reddit, dengan sitasi karangan "via Bloomberg" — lihat §7 poin 3) — Berkshire
  Hathaway ada di sektor Financials, juga di luar universe run ini.

Jadi kasus nyata yang ada BUKAN "persona menjelajahi ticker lain yang sah di dalam universe screening",
melainkan model sesekali menyimpang ke ticker populer/dikenal luas yang lepas dari universe yang sedang
diproses. **Temuan untuk §7:** dari 44 `screened_tickers`, **nol** yang dibahas di luar 4 yang sudah
di-seed lewat `initial_posts` — mekanisme seed adalah **penentu utama** cakupan ticker yang benar-benar
dibahas; model tidak terlihat "menjelajahi" ticker lain di universe yang sama sekali tidak disinggung di
seed. **Implikasi untuk siapa pun yang mendesain event-seeding lebih lanjut (di luar cakupan 11b):**
kalau cakupan ticker yang lebih luas dari universe diinginkan, menambah initial post per ticker tambahan
kemungkinan lebih efektif daripada berharap ticker itu muncul organik — dan validasi keanggotaan universe
(§7 poin 3) tetap wajib supaya AAPL/BRK.A-seperti-ini tidak lolos ke Fase 12 sebagai sinyal yang datanya
sendiri (fundamental/harga) tidak tersedia untuk ticker itu.

---

## 9. Ringkasan untuk Fase 11c (implementasi) — TIDAK dimulai di sini

Dokumen ini **desain saja**. Keputusan yang sudah diambil di §1-7 siap diimplementasikan, tapi beberapa
hal eksplisit diserahkan ke tahap implementasi (bukan diputuskan di sini karena butuh akses ke kode/DB
yang di luar cakupan dokumen desain):

- Engagement count presisi (§5) sebaiknya di-join dari `post`/`comment`/`like`/`repost` table di DB
  platform (ID numerik), bukan parsing teks `actions.jsonl` — detail query SQL adalah pekerjaan 11c.
- Validasi ticker-di-luar-universe (§7 poin 3) perlu regex ekstraksi `$TICKER` yang konsisten dengan
  konvensi normalisasi ticker yang sudah ada (`_normalize_ticker` di `universe.py`, mis. `BRK.A` →
  `BRK-B` mismatch perlu ditangani) — detail normalisasi adalah pekerjaan 11c.
- Prompt persis untuk panggilan batch §3 (index+echo-back+multi-ticker+stance+score) perlu ditulis dan
  diuji seperti `_ENRICHMENT_USER_RULES` Fase 11a — bukan bagian dokumen desain ini.

**Apakah 11b dan 11c digabung jadi satu langkah implementasi, atau 11c tetap terpisah — diserahkan ke
reviewer**, sesuai instruksi eksplisit KONTEKS. Tidak ada implementasi dimulai. Menunggu review.
