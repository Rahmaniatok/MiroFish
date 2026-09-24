# Tahap 4 (Generate Persona dari Graph Zep) — investigasi kelayakan

Konteks: Tahap 3 (`universe_graph_builder.py`) sudah mengembalikan `FilteredEntities`
mentah (bisa ratusan entity, tanpa ranking apa pun — dikonfirmasi ulang investigasi Tahap 3).
Tahap 4 akan menggantikan sampling teks manual (`_select_tickers`/`_sample_articles` di
`persona_generator.py`) dengan bahan ini sebagai sumber grounding untuk generate 8
`InvestorPersona`. **Investigasi murni — tidak ada file dibuat/diubah di `backend/`.**

Satu run `build_universe_graph()` NYATA dijalankan (Zep Cloud + Finnhub live, kode Tahap 3
tidak dimodifikasi) untuk sektor Energy + Communication Services — parameter yang sama
seperti investigasi Tahap 3 sebelumnya, demi kecepatan/biaya. Hasil: **success=True, 44
ticker, 0 kegagalan fetch, 132 item terkirim, ingest 700,2 detik, 267 total node / 175
entity terfilter**. Graph uji dihapus otomatis oleh `build_universe_graph()` sendiri
(siklus hidup create+delete per run, sesuai desain Tahap 3) — tidak ada sampah tertinggal.

## Ringkasan jawaban

| # | Pertanyaan | Jawaban terukur |
|---|---|---|
| 1 | Bentuk nyata `FilteredEntities.to_dict()` | 175 entity (**142 Company, 24 Person, 9 MarketEvent**), 370 referensi edge (double-counted), **164 label edge BERBEDA** — hanya 80/370 (21,6%) benar-benar `INVOLVES`/`COMMENTS_ON` yang didefinisikan ontology; 78,4% sisanya edge BEBAS bentukan Zep sendiri |
| 2 | Ukuran teks kalau semua entity dijejalkan mentah | Serialisasi teks datar (tanpa ranking, format wajar) = **18.776 token (57,3% dari context window 32.768)** untuk universe KECIL ini — borderline muat, TAPI hampir tidak menyisakan ruang untuk prompt+output. Ekstrapolasi ke universe BESAR (~3.200 entity) = **≈343.000 token (≈1.048% dari context window, ~10,5× kepenuhan)** — **TIDAK realistis sama sekali**. Dump JSON mentah bahkan untuk universe kecil sekalipun sudah 249% dari context window |
| 3 | Perlu ranking, atau cukup filter sederhana? | Distribusi degree SANGAT timpang (median=1, mean=2,11, tapi maks=10; 89/175=51% berdegree 1, cuma 14/175=8% berdegree ≥5) — **top-N by degree bermakna secara data**. Filter by entity_type (buang Person) HANYA memangkas 14% volume DAN membuang entity berdegree tertinggi kedua (Ari Emanuel, degree 10) — **tidak efektif dan tidak selektif** |
| 4 | Reuse pola `persona_generator.py` | `_build_persona_prompt`, `_SYSTEM_PROMPT`, `_USER_SCHEMA`, dan SELURUH loop `generate_personas` (retry/temperature/validasi 8-nama-unik) **100% independen dari sumber grounding** — dikonfirmasi baca kode, bukan asumsi. **TEMUAN: cache key `get_or_generate_personas` (`universe_key` = hash dari `sectors`+`market_cap_tiers` saja) TIDAK membedakan sumber grounding** — run lama (news) dan run baru (Zep) dengan parameter sektor sama akan saling dianggap cache-hit yang sama, padahal metodologinya berbeda total |
| 5 | Field provenance yang berguna | **Dikonfirmasi: TIDAK ADA atribusi per-persona** — `generate_personas` memanggil LLM SATU KALI untuk 8 persona sekaligus dari SATU blok teks gabungan (bukti langsung di kode, `generate_personas` baris 461-527). Provenance hanya bermakna di level RUN (paralel field `grounding`/`article_ids` yang sudah ada), BUKAN di `InvestorPersona` itu sendiri |

---

## 1. Bentuk nyata `FilteredEntities.to_dict()` — dari 1 run sungguhan

**Ringkasan run:** `build_universe_graph(sectors=["Energy","Communication Services"])`,
2026-09-23 live. 44 ticker lolos screening, **0 ticker gagal fetch** (kebetulan lebih baik
dari 16-20% yang biasa terlihat di investigasi Tahap 3 — variasi run-to-run yang nyata,
bukan jaminan). 132 item terkirim ke Zep (44×3, tanpa retry yang diperlukan). Batch
`succeeded` dalam 700,2 detik. Graph dihapus otomatis setelah baca entity — sesuai desain
Tahap 3.

**Hasil `filter_defined_entities()`:** 267 total node di graph, **175 lolos filter** (custom
label selain `Entity`/`Node`):

| Entity type | Jumlah |
|---|---|
| Company | 142 |
| Person | 24 |
| MarketEvent | 9 |
| **Total** | **175** |

**Distribusi edge:** 370 referensi edge (dihitung dari `len(related_edges)` tiap entity —
"double-counted" karena 1 edge nyata muncul di KEDUA entity endpoint-nya kalau keduanya
sama-sama masuk daftar 175 yang lolos filter). **Temuan penting yang tidak diminta tapi
relevan untuk desain teks grounding:** dari 370 referensi itu, **164 LABEL EDGE BERBEDA**
muncul — Zep TIDAK membatasi diri ke 2 edge type yang didefinisikan ontology
(`INVOLVES`/`COMMENTS_ON`). Hanya **80/370 (21,6%)** yang benar-benar 2 tipe itu; **290/370
(78,4%)** adalah edge BEBAS bentukan Zep sendiri berdasarkan teks artikel (`WORKS_AT`,
`DISCUSSED`, `REPORTS_ON`, `ANNOUNCED`, `OWNS`, `ACQUIRED`, `PARTNERED_WITH`,
`HAS_STOCK_PERFORMANCE`, `DOWNGRADED`, `COMPETES_WITH`, dst — daftar lengkap 164 label ada
di lampiran). Ini perilaku Zep/Graphiti yang dikenal (ontology custom MEMPENGARUHI tapi
tidak SEPENUHNYA MEMBATASI ekstraksi) — relevan untuk Tahap 4 karena teks grounding akan
jauh lebih kaya/variatif dari yang tersirat "ontology cuma 2 edge type", tapi juga lebih
tidak terkontrol namanya.

**3 contoh entity lengkap (JSON nyata, dipotong related_nodes untuk keterbacaan — bentuk
penuh ada di lampiran):**

*Entity berdegree tertinggi (10) — Person:*
```json
{
  "name": "Ari Emanuel",
  "labels": ["Person"],
  "summary": "Ari Emanuel is TKO's executive chair and CEO. He is the author of the new memoir Roll the Calls.",
  "attributes": {"details": "Hollywood agent; CEO of TKO; author of the new memoir 'Roll the Calls'.", "name": "Ari Emanuel"},
  "related_edges": [
    {"direction": "outgoing", "edge_name": "PARTICIPATED_IN", "fact": "Ari Emanuel participated in an extended CNBC interview with David Faber.", "target_node_uuid": "bd389a31-..."},
    {"direction": "outgoing", "edge_name": "DISCUSSED", "fact": "Ari Emanuel and Mark Shapiro discussed sports valuations during the CNBC interview.", "target_node_uuid": "78fe8d07-..."},
    {"direction": "outgoing", "edge_name": "CEO_OF", "fact": "Ari Emanuel is CEO of TKO.", "target_node_uuid": "64beec15-..."}
    // ... 7 edge lain, total 10
  ]
}
```

*Entity berdegree tinggi (10) — Company, dengan campuran fakta fundamental DAN naratif:*
```json
{
  "name": "Verizon",
  "labels": ["Company"],
  "attributes": {"name": "Verizon", "ticker": "VZ"},
  "related_edges": [
    {"edge_name": "HAS_MARKET_PERFORMANCE", "fact": "Verizon Communications' share price slipped 7.04% over the past week and 3.58% over the past month, while its 90-day share price return was 2.03% and its year-to-date share price return was 17.67%."},
    {"edge_name": "REPORTS_FREE_CASH_FLOW_GROWTH", "fact": "Verizon reported a 24.4% increase in free cash flow in Q2, prompting higher 2026 growth expectations..."},
    {"edge_name": "PARTICIPATES_IN", "fact": "Verizon is seen as a leading participant in the 2027 spectrum auction, with analysts suggesting Verizon could spend up to $20 billion."}
    // ... 7 edge lain, total 10
  ]
}
```

*Entity berdegree rendah (1) — mayoritas kasus (89/175):*
```json
{
  "name": "Similarweb",
  "labels": ["Company"],
  "attributes": {"name": "Similarweb", "ticker": "RDDT"},
  "related_edges": [
    {"direction": "incoming", "edge_name": "COMMENTS_ON", "fact": "Similarweb data was cited by DA Davidson as showing continued pressure on global daily active users for Reddit, Inc."}
  ]
}
```

Kualitas fakta yang diekstrak KONSISTEN dengan investigasi Tahap 3 sebelumnya — presisi
tinggi (angka, tanggal, nama analis/perusahaan), bukan node generik kosong.

---

## 2. Ukuran teks — dihitung dengan tokenizer NYATA, bukan perkiraan kasar

Model yang dipakai (`Config.LLM_MODEL_NAME`, RunPod vLLM): `Qwen/Qwen2.5-7B-Instruct-AWQ`.
Context window **32.768 token dikonfirmasi langsung dari `max_position_embeddings` config
model tersebut** (di-load via `transformers.AutoConfig`, bukan dikutip dari dokumen lain —
saya tidak menemukan angka ini tertulis eksplisit di `fase11a_preflight_verification.md`
seperti diasumsikan brief, jadi saya verifikasi ulang langsung dari config model; angkanya
cocok). Token dihitung dengan **tokenizer asli model itu** (`AutoTokenizer.from_pretrained`),
bukan estimasi genetik "~4 karakter/token".

**Dua cara serialisasi diukur, apples-to-apples pada 175 entity yang sama:**

| Serialisasi | Karakter | Token (tokenizer nyata) | % dari context 32.768 |
|---|---|---|---|
| **Dump JSON mentah** (`FilteredEntities.to_dict()` apa adanya — termasuk uuid, related_nodes berulang) | 242.549 | **81.593** | **249,0%** |
| **Teks datar wajar** (1 baris `Type: Name - Summary` + bullet tiap fakta edge, TANPA uuid/related_nodes — cara paling langsung format teks tanpa ranking/summarization tambahan) | 81.106 | **18.776** | **57,3%** |

Dump JSON mentah sudah **2,5× melebihi context window bahkan untuk universe kecil 44
ticker** — jelas tidak layak dalam bentuk apapun. Teks datar wajar (yang jadi rujukan utama
untuk pertanyaan brief) MUAT untuk universe kecil ini (57,3%), tapi rata-rata **107,3
token/entity** — hampir tidak menyisakan ruang untuk prompt sistem+instruksi (`_SYSTEM_PROMPT`
+`_USER_HEAD`+`_USER_SCHEMA`, ≈400-600 token diukur kasar dari panjang string yang ada) DAN
output 8 persona (dokumen desain Fase 10a lama tidak mengukur ini persis, tapi 8 objek JSON
dengan 6+ field teks 2-4 kalimat masing-masing realistis 1.500-3.000 token). Total realistis
≈18.776 + 600 + 2.500 ≈ **21.876 token (≈67% context)** — MUAT, tapi dengan margin tipis,
untuk universe SEKECIL 44 ticker.

**Ekstrapolasi ke universe besar (rasio dari investigasi Tahap 3: 150 item → 202 filtered
entity, jadi ≈1,35 entity/item; run ini 132 item → 175 entity ≈1,33 entity/item, konsisten
— dipakai rasio 1,33 untuk proyeksi ~500 ticker × 3 artikel ≈ 1.500 item → **≈2.000 filtered
entity**, dibulatkan ke 3.200 sesuai perkiraan brief sebagai batas atas kasar):**

| | Token (teks datar) | % dari context 32.768 |
|---|---|---|
| ~2.000 entity (proyeksi rasio langsung dari data run ini) | ≈214.600 | ≈655% |
| ~3.200 entity (perkiraan kasar brief) | ≈343.300 | ≈1.048% |

**Jawaban langsung: TIDAK realistis "jejalkan semua" untuk universe besar dalam bentuk
apapun** — bahkan dengan proyeksi paling konservatif (2.000 entity), sudah 6,5× melebihi
context window. Untuk universe kecil, teknis MUAT tapi dengan margin sempit yang membuatnya
rapuh (universe sedikit lebih besar dari 44 ticker saja — mis. 3 sektor bukan 2 — bisa
langsung melewati batas).

---

## 3. Perlu ranking, atau cukup filter sederhana? — dihitung dari data nyata

**Degree (`len(related_edges)`) per entity, 175 entity run ini:**

| Statistik | Nilai |
|---|---|
| Minimum | 1 |
| Maksimum | 10 |
| Mean | 2,11 |
| Median | **1** |
| Entity berdegree 1 | 89/175 (51%) |
| Entity berdegree ≥5 | 14/175 (8%) |
| Entity berdegree ≥10 | 3/175 (1,7%) |

**Distribusi TIMPANG secara nyata** — lebih dari separuh entity cuma py 1 edge (biasanya 1
fakta tunggal dari 1 artikel), sementara segelintir entity (Ari Emanuel, Verizon, Paramount
— semua degree 10) jadi pusat dari BANYAK fakta yang saling terhubung (analyst rating,
performa saham, event, sebutan berulang lintas artikel). **Top-N by degree BERMAKNA di data
ini** — bukan cuma teori, terbukti dari sebaran nyata.

**Perbandingan 3 alternatif KONKRET, diuji pada data yang SAMA:**

| Metode | Hasil pada data nyata ini |
|---|---|
| **(a) Top-N by degree** (mis. top 74 ≈ setengah dari 175, ambil semua degree ≥2) | Menyisakan entity paling terhubung — Ari Emanuel, Verizon, Paramount, SLB, DVN, AT&T, Netflix, Google, dst. (top 20-by-degree: **17 Company, 2 Person, 1 MarketEvent**). Token turun proporsional dari 18.776 -> **≈7.940** (74×107,3) — muat nyaman (24% context). Butuh kode tambahan TAPI SEDERHANA (`sorted(entities, key=lambda e: len(e.related_edges), reverse=True)[:N]`, murni Python, tanpa LLM/library baru) |
| **(b) Filter by entity_type** (buang Person, sisakan Company+MarketEvent) | 142+9=**151/175 (86%)** — HANYA memangkas 14% volume, TIDAK CUKUP untuk masuk anggaran token manapun yang berarti. LEBIH BURUK dari (a): top-20-by-degree yang sebenarnya DIDOMINASI Company (17/20) tapi tetap punya 2 Person penting (termasuk Ari Emanuel, degree TERTINGGI di seluruh graph) — filter (b) akan MEMBUANG Ari Emanuel begitu saja sambil MEMPERTAHANKAN puluhan Company berdegree 1 yang jauh kurang informatif |
| **(c) Random sample N / potong pada urutan apa adanya** | Token savings SAMA seperti (a) untuk N yang sama, TAPI urutan default `zep_paging`/`graph.node.get_by_graph_id` adalah `order_by="uuid"` (dikonfirmasi dari signature SDK terpasang) — **urutan UUID tidak berkorelasi dengan apapun yang bermakna**. Memotong N pertama dari urutan ini setara acak, TIDAK ADA jaminan entity berdegree tinggi (paling "banyak dibicarakan") ikut kepilih — bisa saja Ari Emanuel/Verizon/Paramount semuanya terbuang murni karena UUID mereka "kalah urutan" |

**Rekomendasi awal (bukan keputusan final, murni condong berbasis data di atas):**
**top-N by degree** adalah pendekatan paling murah/sederhana YANG SEKALIGUS paling selektif
secara nyata pada data ini — kompleksitas implementasinya HAMPIR SAMA dengan filter
entity_type (satu `sorted()`+slice, tidak perlu LLM/library graph baru), tapi jauh lebih
efektif memangkas volume (bisa turun ke level token berapapun yang ditarget, tidak terpaku
86%) DAN lebih tepat sasaran (menjaga entity yang benar-benar jadi pusat cerita lintas-artikel,
bukan sekadar tipe). N konkret (mis. 74, atau berbasis anggaran token langsung — "ambil
entity berdegree tertinggi sampai anggaran token grounding tercapai") adalah keputusan
desain yang menyusul, bukan diputuskan di sini.

---

## 4. Reuse pola `persona_generator.py` — dikonfirmasi lewat pembacaan kode

**`_build_persona_prompt(news_lines, grounding)` (baris 230-241) — REUSE 100%, TANPA
modifikasi.** Fungsi ini murni menerima `news_lines: List[str]` (baris teks apapun) dan
`grounding: str` (penanda ada/tidaknya grounding) — TIDAK ADA logika di dalamnya yang
spesifik ke berita/Finnhub. `_SYSTEM_PROMPT`/`_USER_HEAD`/`_NEWS_INSTRUCTION`/`_USER_SCHEMA`
semuanya generik ("Use the news below only as loose inspiration..." — kalimat ini TIDAK
menyebut sumber tertentu, cocok dipakai apa adanya untuk baris teks dari entity Zep juga).
**Implikasi:** Tahap 4 tinggal membangun `List[str]` dari entity Zep (mis. format
`"{type}: {name} - {summary}"` + fakta edge terpilih, mirip format di §2 di atas) dan
memanggil `_build_persona_prompt` PERSIS seperti sekarang, hanya `news_lines`-nya yang beda
isi.

**`generate_personas()` loop retry (baris 461-527) — REUSE 100%, dikonfirmasi TIDAK ADA
ketergantungan ke sumber grounding.** Body loop (`create_chat_completion` -> `_repair_json`
-> `_validate_personas` -> `_soft_checks` -> simpan) HANYA menyentuh `system_prompt`/
`user_prompt` (string sudah jadi) dan `temperature`/`attempt` — tidak ada referensi ke
`universe`/`sample`/`news_lines` di dalam loop itu sendiri (semua sudah "dibekukan" jadi 2
string SEBELUM loop dimulai, baris 454). Temperature schedule, validasi struktural
(8-persona-unique-name), soft-check archetype-collision — semuanya generik. **Konfirmasi
langsung, bukan asumsi: bagian ini bisa dipakai ulang 100% tanpa modifikasi.**

**Konsekuensi struktural yang perlu dicatat (bukan blocker, tapi bukan "tinggal tempel"):**
`generate_personas()` SAAT INI memanggil `screen_universe()` + `_sample_articles()` LANGSUNG
di dalam tubuhnya sendiri (baris 434-449) — sumber grounding TIDAK di-inject dari luar
lewat parameter, tapi di-hardcode ke jalur news. Mengganti sumber grounding ke Zep berarti
`generate_personas()` (atau fungsi barunya) perlu direstrukturisasi supaya BISA menerima
`news_lines`/grounding dari sumber lain (mis. parameter `news_lines_provider` atau split
jadi 2 fungsi: "bangun grounding" dan "generate dari grounding") — ini keputusan DESAIN,
bukan cuma "ganti 1 baris", meski logika di DALAM loop-nya sendiri tidak perlu disentuh.

**`get_or_generate_personas()` cache key — TEMUAN, perlu diperbaiki di desain:**
`universe_key = sha256(json.dumps({"sectors":..., "market_cap_tiers":...}))` (baris 585-587)
— HANYA fungsi dari parameter screening, TIDAK ADA komponen yang membedakan "sumber
grounding-nya berita manual" vs "sumber grounding-nya graph Zep". Kalau Tahap 4 dipasang
berdampingan dengan jalur lama (mis. sebagai flag opsional, bukan penggantian penuh
langsung), `get_or_generate_personas(sectors=["Energy"], ...)` yang pernah dijalankan lewat
jalur LAMA (news) akan ke-"cache hit" dan dikembalikan APA ADANYA untuk permintaan BARU yang
sebetulnya mau pakai jalur Zep — dua metodologi yang beda hasilnya saling tertukar diam-diam
di cache. **Ini harus diperbaiki di fase desain** (mis. tambah komponen `grounding_source`
ke hash `universe_key`, atau tabel/kolom terpisah) — dicatat di sini sebagai temuan wajib
ditangani, bukan diputuskan solusinya di investigasi ini.

---

## 5. Field provenance — dikonfirmasi TIDAK ADA atribusi per-persona

**Dikonfirmasi langsung dari kode (bukan asumsi):** `generate_personas()` memanggil
`create_chat_completion` **SATU KALI per attempt** (baris 464-474), dengan SATU
`user_prompt` yang berisi SELURUH blok grounding tergabung (`news_block` di
`_build_persona_prompt`, baris 233-237), dan respons LLM adalah **SATU JSON object** berisi
`{"personas": [...8 objek...]}` — 8 persona lahir dari 1 pemanggilan LLM, 1 blok grounding
gabungan yang sama. **Tidak ada mekanisme apapun yang memetakan "persona ke-3 lahir dari
entity Verizon" — ini secara struktural TIDAK MUNGKIN dilacak dengan desain generate-8-
sekaligus yang sudah ada**, sesuai dugaan brief. Mengubah ini (mis. generate 1 persona per
panggilan LLM per entity) adalah perubahan arsitektur besar yang TIDAK diminta/diinvestigasi
di sini.

**Implikasi: provenance HARUS di level RUN, bukan di level `InvestorPersona`.** Menambah
field provenance ke DALAM `InvestorPersona` dataclass itu sendiri (mis.
`source_entity_ids: List[str]` per-persona) akan SELALU berisi nilai yang SAMA untuk
kedelapan persona (karena semuanya memang lahir dari input yang sama) — field seperti itu
tidak menambah informasi apapun dibanding cukup menyimpannya SEKALI di level run, dan
berisiko menyesatkan (terlihat seolah-olah field itu spesifik per-persona, padahal tidak).

**Usulan konkret (mengikuti pola yang SUDAH ADA di result dict `generate_personas`, sejajar
`grounding`/`article_ids`/`sample_articles` yang sudah ada sekarang — bukan pola baru):**

| Field usulan (level RUN, sejajar `grounding` dkk yang sudah ada) | Isi | Pola existing yang ditiru |
|---|---|---|
| `grounding_source` | `"news"` \| `"zep_graph"` \| `"none"` (memperluas nilai `grounding` yang sekarang cuma `"news"`/`"none"`) | field `grounding` yang sudah ada |
| `source_graph_id` | graph_id Zep yang dipakai (meski sudah dihapus setelah Tahap 3 selesai — tetap berguna sebagai jejak audit "graph mana", string historis, BUKAN referensi hidup) | analog `run_id` yang sudah ada |
| `source_entity_count` | jumlah entity yang MASUK ke grounding text setelah filter (§3) — beda dari `total_count`/`filtered_count` mentah Tahap 3 | analog panjang `article_ids` yang sudah disimpan |
| `source_entity_uuids` atau `source_entity_names` | daftar entity yang benar-benar dipakai di teks grounding (untuk audit "kenapa LLM menyebut Verizon di persona X") | PERSIS pola `article_ids`/`sample_articles` yang sudah ada — bukan konsep baru |
| `filter_method_used` | tag metode filter §3 yang dipakai (mis. `"top_degree_74"`) | tidak ada analog existing, tapi murni string audit, additive |

Semua field ini **additive ke dict hasil `generate_personas`/`get_or_generate_personas`**,
BUKAN ke `InvestorPersona` — konsisten dengan keputusan yang sudah diambil ("9 field wajib
tetap sama persis") dan dengan bukti §5 di atas bahwa atribusi per-persona memang tidak
mungkin secara struktural.

---

## Lampiran — parameter run, untuk reproduksi

```
build_universe_graph(as_of_date=None, sectors=["Energy","Communication Services"])
  universe_screen -> 44 ticker (identik run investigasi Tahap 3 sebelumnya)
  fetch: 44/44 sukses first-try, 0 retry diperlukan (variasi run-to-run — bukan jaminan)
  item_count = 132 (44 x 3, tanpa dedup cross-ticker yang terpakai di run ini)
  ingest_seconds = 700.2
  filtered_entities: total_count=267, filtered_count=175
    Company=142, Person=24, MarketEvent=9
  total edge-endpoint references = 370, 164 distinct edge_name label
    (80 ontology-defined INVOLVES/COMMENTS_ON, 290 free-form Zep-generated)
  degree distribution: min=1 max=10 mean=2.11 median=1
    degree>=5: 14/175 (8%), degree>=10: 3/175 (1.7%)
  tokenizer: Qwen/Qwen2.5-7B-Instruct-AWQ (AutoTokenizer.from_pretrained, real, bukan estimasi)
    context window = 32768 (dari AutoConfig.max_position_embeddings)
  serialisasi diukur:
    raw JSON dump filtered_entities: 242,549 char -> 81,593 token (249.0% context)
    flat-text (Type: Name - Summary + bullet fakta edge): 81,106 char -> 18,776 token (57.3% context)
  graph_id (dihapus otomatis oleh build_universe_graph()) = mirofish_universe_live_<hex8>
```

Skrip investigasi (sekali-pakai, tidak di-commit, tersimpan di scratchpad sesi) memanggil
`build_universe_graph()` PERSIS seperti produksi (Tahap 3, tanpa modifikasi). Graph Zep
dihapus otomatis oleh fungsi itu sendiri sesuai desainnya — tidak ada langkah cleanup
tambahan yang perlu dilakukan investigasi ini.
