# Tahap 4 (Generate Persona dari Graph Zep) — Desain

Status: **desain, belum implementasi** — menunggu review. JANGAN mulai implementasi
setelah dokumen ini selesai.

Konteks: [docs/investigations/tahap4_persona_from_graph.md](../investigations/tahap4_persona_from_graph.md)
(2026-09-23, data nyata: 175 entity, tokenizer `Qwen/Qwen2.5-7B-Instruct-AWQ` asli) sudah
menjawab kelayakan. Dokumen ini mengambil temuannya dan memutuskan desain konkret.

**Scope dokumen ini murni desain.** Tidak ada file diubah. Referensi yang dibaca ulang
penuh untuk memastikan signature cocok persis dengan kode nyata: `persona_generator.py`,
`universe_graph_builder.py`.

## Keputusan yang sudah diambil (given)

- **Anggaran token DINAMIS untuk grounding** (bukan N-entity tetap): urutkan entity by
  degree tertinggi→terendah, masukkan satu per satu sambil menghitung token berjalan,
  **stop begitu entity berikutnya akan melebihi budget 15.000 token**. Universe kecil yang
  totalnya sudah di bawah 15.000 → masukkan semua.
- Skema `InvestorPersona` (9 field wajib): **TIDAK BERUBAH**.
- Provenance: **HANYA di level RUN** (dict hasil `generate_personas`), TIDAK ADA field baru
  di `InvestorPersona` itu sendiri — dikonfirmasi struktural tidak mungkin per-persona.
- `_build_persona_prompt` dan loop `generate_personas` (retry/temperature/validasi
  8-nama-unik): **REUSE 100%, isi TIDAK dimodifikasi** — hanya cara memanggilnya berubah.

---

## 1. Restrukturisasi `generate_personas()` — Opsi (a), dengan alasan

**Opsi (a) dipilih.** Split jadi builder grounding (2 fungsi, sumber berbeda, bentuk return
SAMA) + `generate_personas` generik yang menerima grounding SUDAH JADI sebagai parameter.

**Alasan (bukan sekadar preferensi gaya):**
- Opsi (b) (parameter `grounding_source` bercabang DI DALAM `generate_personas`) akan
  menambah `if grounding_source == "news": ... elif == "zep_graph": ...` DI DALAM fungsi
  yang sama tempat loop retry/validasi berada — bertentangan LANGSUNG dengan syarat
  investigasi ("body loop retry/validasi BENAR-BENAR tidak tersentuh"). Walaupun
  percabangan itu bisa ditaruh SEBELUM loop dimulai (secara teknis loop-nya sendiri tetap
  tidak tersentuh), tetap menambah kompleksitas siklomatik ke fungsi yang SUDAH punya
  tanggung jawab besar (retry+temperature+validasi+persistence-trigger) — melanggar
  pemisahan tanggung jawab tanpa alasan kuat.
- Opsi (a) membuat `generate_personas` **murni fungsi dari grounding yang sudah jadi** —
  tidak tahu, tidak peduli, dan tidak pernah perlu tahu dari mana asal `news_lines`-nya.
  Ini juga PERSIS bentuk yang sudah ADA sebagian: `_sample_news_for_grounding` (baris
  157-168 `persona_generator.py`, **sudah ADA di kode, TIDAK DIPAKAI `generate_personas`
  saat ini** — `generate_personas` justru mengulang logikanya inline) SUDAH mengembalikan
  bentuk `(List[str], str)` yang persis diminta Opsi (a). Temuan ini artinya Opsi (a) bukan
  pola baru yang dipaksakan — ini pola yang SUDAH ada tapi belum dipakai konsisten.

**Penyesuaian atas bentuk return yang diminta:** brief meminta bentuk return
`(news_lines: List[str], grounding: str)` untuk kedua builder. Dari pembacaan ulang
`generate_personas` (baris 449-453), fungsi itu JUGA butuh `article_ids`/`sample_articles`
(provenance jalur news) yang diturunkan dari `sample` MENTAH — bukan cuma dari
`news_lines`/`grounding` yang sudah diformat. Kalau builder cuma mengembalikan 2-tuple
polos, `generate_personas` akan terpaksa MEMANGGIL ULANG logika sampling untuk dapat
provenance-nya — mengulang duplikasi yang justru ingin dihilangkan Opsi (a). **Diputuskan:
tambahkan 1 field lagi (provenance) DAN 1 field penanda sumber (`source`), dibungkus dalam
1 dataclass, bukan tuple polos** — tetap "bentuk sama persis" antar kedua builder (sama-sama
`GroundingResult`), hanya lebih tahan future-proof daripada tuple mentah:

```python
@dataclass
class GroundingResult:
    news_lines: List[str]       # baris teks siap masuk _build_persona_prompt
    grounding: str               # "news" | "zep_graph" | "none" -- persis nilai yang
                                  # sudah dipakai _build_persona_prompt hari ini
    source: str                  # "news" | "zep_graph" -- SELALU sumber builder yang
                                  # dipanggil, bahkan saat grounding=="none" (beda dari
                                  # `grounding`: field ini menjawab "builder mana yang
                                  # dicoba", bukan "apakah hasilnya ada isinya")
    provenance: Dict[str, Any]   # isi BEDA per sumber -- lihat §5
```

---

## 2. `build_grounding_from_graph` — anggaran token dinamis

### Signature

```python
ZEP_GROUNDING_TOKEN_BUDGET = 15_000  # lihat §2 investigasi: 500 (prompt) + 15.000
                                       # (grounding) + ~3.000 (output 8 persona)
                                       # = 18.500 token ~= 56,5% dari context 32.768

def build_grounding_from_graph(
    filtered_entities: Dict[str, Any],
    token_budget: int = ZEP_GROUNDING_TOKEN_BUDGET,
) -> GroundingResult:
    ...
```

`filtered_entities` adalah bentuk `FilteredEntities.to_dict()` PERSIS seperti yang
dikembalikan Tahap 3 (`result["filtered_entities"]` dari `build_universe_graph()`) — fungsi
ini **HANYA butuh bentuk dict-nya**, TIDAK import `universe_graph_builder.py` atau
`GraphBuilderService`/`ZepEntityReader` apapun — kopling murni ke bentuk data, bukan ke
mekanisme Zep API.

### Tokenizer — dipilih EKSPLISIT, dimuat SEKALI (module-level singleton)

```python
_GROUNDING_TOKENIZER_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct-AWQ"  # HARUS disamakan manual
    # kalau model deployment (Config.LLM_MODEL_NAME) berganti -- lihat catatan di bawah
_grounding_tokenizer = None  # lazy singleton, di-set sekali per proses

def _get_grounding_tokenizer():
    global _grounding_tokenizer
    if _grounding_tokenizer is None:
        from transformers import AutoTokenizer
        _grounding_tokenizer = AutoTokenizer.from_pretrained(_GROUNDING_TOKENIZER_MODEL_ID)
    return _grounding_tokenizer
```

**Kenapa dimuat sekali, bukan tiap panggilan:** diukur langsung, `AutoTokenizer.from_pretrained`
makan **6,3 detik cold / 1,74 detik bahkan dengan cache HuggingFace lokal sudah hangat**
(diukur di lingkungan investigasi ini). Memuat ulang tiap panggilan `build_grounding_from_graph`
menambah overhead murni 1,7-6,3 detik per run — kecil dibanding total waktu Tahap 3
(menit-jam), TAPI tidak ada alasan menanggungnya berulang kali kalau proses yang sama bisa
memanggil fungsi ini lebih dari sekali (mis. retry di level orkestrasi, atau backtest
berurutan dalam 1 proses long-running). Singleton module-level menghindari ini sepenuhnya
setelah pemanggilan pertama.

**3 catatan operasional yang WAJIB ditangani sebelum implementasi (bukan diputuskan di sini,
ditandai sebagai prasyarat):**
1. `_GROUNDING_TOKENIZER_MODEL_ID` **DI-HARDCODE**, TIDAK diturunkan dari
   `Config.LLM_MODEL_NAME` (yang default-nya `'gpt-4o-mini'`, bukan repo HuggingFace yang
   valid — `AutoTokenizer.from_pretrained` akan gagal kalau langsung dipakai untuk model
   OpenAI). Konsekuensi: kalau deployment model LLM berganti dari Qwen2.5-7B ke model lain,
   konstanta ini harus di-update MANUAL — tidak otomatis mengikuti. Untuk model non-Qwen
   (mis. fallback ke OpenAI), angka token dari tokenizer Qwen ini tetap dipakai sebagai
   PROKSI perkiraan (beda tokenizer biasanya cuma selisih ~10-20% untuk teks Inggris) —
   cukup aman karena `token_budget=15.000` sudah longgar (57% dari context), bukan mepet
   99%, tapi ini asumsi yang harus disadari, bukan jaminan matematis.
2. **`transformers` SAAT INI bukan dependency LANGSUNG** `pyproject.toml`/`requirements.txt`
   — hanya terpasang TRANSITIF lewat `sentence-transformers` (dikonfirmasi lewat
   `uv.lock`). Kalau rantai dependency transitif itu berubah di masa depan (mis.
   `camel-oasis` melepas `sentence-transformers`), import ini bisa diam-diam gagal.
   **Rekomendasi: deklarasikan `transformers` (atau `tokenizers` saja kalau cukup ringan)
   sebagai dependency LANGSUNG** saat implementasi — murah dilakukan, mencegah kegagalan
   silent nanti.
3. `AutoTokenizer.from_pretrained` pada pemanggilan PERTAMA (tanpa cache HuggingFace lokal
   sebelumnya) **butuh akses internet ke huggingface.co** untuk mengunduh file tokenizer.
   Deployment produksi tanpa akses internet keluar (atau image Docker yang di-build offline)
   akan GAGAL di panggilan pertama kecuali file tokenizer sudah di-vendor/pre-warmed ke
   image. **Ini prasyarat operasional yang harus ditangani saat implementasi/deployment**,
   bukan diselesaikan di dokumen desain ini.

### Format teks per entity — PERSIS format investigasi (sudah divalidasi dengan tokenizer nyata)

```python
def _format_entity_text(entity: Dict[str, Any]) -> str:
    custom_labels = [l for l in entity.get("labels", []) if l not in ("Entity", "Node")]
    entity_type = custom_labels[0] if custom_labels else "Entity"
    lines = [f"{entity_type}: {entity['name']} - {entity.get('summary') or ''}"]
    for edge in entity.get("related_edges") or []:
        lines.append(f"  - {edge.get('edge_name', '')}: {edge.get('fact', '')}")
    return "\n".join(lines)
```

**Semua `related_edges` milik entity yang lolos anggaran DIMASUKKAN UTUH, TIDAK dipotong
sebagian** — keputusan disengaja: kontrol anggaran terjadi di level SELURUH-ENTITY
("masuk semua faktanya, atau tidak masuk sama sekali"), bukan di level per-fakta di dalam
entity yang sudah terpilih. Alasan: entity berdegree tinggi dipilih JUSTRU karena
fakta-faktanya banyak dan saling terhubung (§3 investigasi) — memotong sebagian faktanya
setelah terpilih melemahkan alasan awal ia dipilih, dan menambah 1 lapis logika pemotongan
lagi di atas logika stop-per-entity yang sudah ada (kompleksitas ganda tanpa manfaat jelas).

### Algoritma pengisian

```python
def build_grounding_from_graph(
    filtered_entities: Dict[str, Any],
    token_budget: int = ZEP_GROUNDING_TOKEN_BUDGET,
) -> GroundingResult:
    entities = filtered_entities.get("entities") or []
    empty = GroundingResult(
        news_lines=[], grounding="none", source="zep_graph",
        provenance={
            "source_entity_count": 0, "source_entity_names": [],
            "source_grounding_tokens": 0, "filter_method_used": "top_degree_token_budget",
        },
    )
    if not entities:
        return empty  # EDGE CASE (Tahap 3 sukses, 0 entity lolos filter): "none", bukan error

    ranked = sorted(entities, key=lambda e: len(e.get("related_edges") or []), reverse=True)
    tokenizer = _get_grounding_tokenizer()

    news_lines: List[str] = []
    included_names: List[str] = []
    total_tokens = 0

    for entity in ranked:
        text = _format_entity_text(entity)
        entity_tokens = len(tokenizer.encode(text))
        if total_tokens > 0 and total_tokens + entity_tokens > token_budget:
            break  # STOP -- entity ini TIDAK dimasukkan, bukan dipotong sebagian
        news_lines.append(text)
        included_names.append(entity["name"])
        total_tokens += entity_tokens
        # `total_tokens > 0` di baris `if` atas SENGAJA membuat entity PERTAMA (degree
        # tertinggi) SELALU masuk, WALAU sendirian sudah melebihi token_budget -- lihat
        # penjelasan EDGE CASE 2 di bawah.

    if not news_lines:
        return empty  # tidak realistis tercapai (lihat EDGE CASE 2), defensif saja

    return GroundingResult(
        news_lines=news_lines,
        grounding="zep_graph",
        source="zep_graph",
        provenance={
            "source_entity_count": len(news_lines),
            "source_entity_names": included_names,
            "source_grounding_tokens": total_tokens,
            "filter_method_used": "top_degree_token_budget",
        },
    )
```

**EDGE CASE 1 (universe kecil, semua entity muat):** loop menyelesaikan seluruh `ranked`
tanpa pernah `break` — SEMUA entity masuk `news_lines`. Ini otomatis dari algoritma di atas
(tidak butuh percabangan khusus) — persis kasus 175-entity investigasi ini (18.776 token
untuk SEMUA 175 masih di bawah kalau `token_budget` diset ≥18.776; dengan
`token_budget=15.000` yang dipilih, universe INI SPESIFIK akan berhenti sebelum entity
ke-175 — lihat estimasi §"Contoh dict lengkap" di bawah).

**EDGE CASE 2 (1 entity sendirian melebihi SELURUH budget):** ditangani lewat guard
`total_tokens > 0` di kondisi `if` — entity PERTAMA (degree tertinggi, berarti kandidat
PALING PENTING secara data §3 investigasi) **SELALU dimasukkan apa adanya, walau sendirian
sudah melebihi `token_budget`**, dengan konsekuensi total token grounding BISA sedikit
melebihi 15.000 di kasus ekstrem ini. **Alasan:** entity berdegree tertinggi adalah "cerita
utama" universe hari itu (kasus nyata: Ari Emanuel/Verizon/Paramount, semua degree 10) —
membuang entity itu SELURUHNYA demi menghormati batas token secara ketat adalah trade-off
yang lebih buruk daripada sedikit melebihi anggaran. Entity KEDUA dan seterusnya tetap
tunduk pada aturan stop-ketat biasa. **Realistis TIDAK PERNAH terpicu** dengan skala entity
yang terlihat sejauh ini (entity terbesar di data nyata cuma 387 token, jauh dari 15.000) —
tapi kode tetap harus benar untuk kasus ekstrem yang belum pernah terlihat.

**`grounding` bernilai `"none"` pada 2 kondisi:** `filtered_entities["entities"]` kosong
total (Tahap 3 sukses tapi 0 entity lolos filter — kasus ekstrem tapi valid), ATAU (secara
teori, tidak realistis tercapai) tidak ada satupun entity yang berhasil masuk `news_lines`.
Paralel PERSIS dengan makna `grounding="none"` yang sudah ada di jalur news (`_grounding_for`,
<10 artikel).

---

## 3. Perbaikan cache key — WAJIB, backward-compatible

**Field `source` di `GroundingResult` (§1) ADALAH jawaban untuk "parameter eksplisit atau
di-derive":** bukan parameter terpisah yang harus diketik ulang caller, bukan pula
di-derive diam-diam dari ada/tidaknya argumen lain — **field eksplisit di dalam objek
`GroundingResult` yang sudah caller pegang** (karena caller sudah memanggil salah satu
builder untuk mendapatkannya). Ini jalan tengah yang menghindari duplikasi
(`generate_personas(grounding, grounding_source=...)` akan redundan — `grounding_source`
selalu sama dengan `grounding.source`) SEKALIGUS menghindari derivasi implisit yang rapuh.

**Hash gabungan, BUKAN hash terpisah — TAPI backward-compatible untuk `source="news"`:**

```python
def _screening_params(
    sectors: Optional[List[str]],
    market_cap_tiers: Optional[List[str]],
    grounding_source: str = "news",
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "sectors": sorted(set(sectors)) if sectors is not None else None,
        "market_cap_tiers": sorted(set(market_cap_tiers)) if market_cap_tiers is not None else None,
    }
    if grounding_source != "news":
        # "news" TIDAK menambah key baru -- hash-nya PERSIS sama seperti sebelum Tahap 4
        # ada, supaya persona_runs lama (semuanya jalur news, ditulis sebelum field ini
        # ada) TETAP valid sebagai cache hit. Sumber grounding lain (mis. "zep_graph")
        # menambah key ini sehingga TIDAK PERNAH menghasilkan hash yang sama dengan
        # kombinasi sectors/tiers identik di jalur "news" -- tabrakan yang jadi temuan
        # investigasi §4 SECARA STRUKTURAL tidak mungkin terjadi lagi.
        params["grounding_source"] = grounding_source
    return params
```

`_universe_key(screening)` **TIDAK PERLU DIUBAH SAMA SEKALI** — ia cuma
`sha256(json.dumps(screening, sort_keys=True))`, sudah generik terhadap isi dict apapun
yang diberikan. Ini PERSIS "1 hash gabungan" yang diminta, bukan 2 hash terpisah yang
digabung manual.

**Konsekuensi yang disadari:** run `grounding_source="news"` untuk sektor yang SAMA PERSIS
tetap 100% backward-compatible (hash identik ke skema lama) — TIDAK ada data lama yang
"hilang" dari cache. Run `grounding_source="zep_graph"` SELALU dapat `universe_key` baru
yang belum pernah ada di tabel manapun (karena field `grounding_source` di payload hash
tidak pernah muncul di data lama) — pertama kali dipanggil untuk sektor apapun PASTI cache
miss (generate baru), sesuai ekspektasi wajar untuk metodologi yang baru diperkenalkan.

---

## 4. Orkestrasi level atas — Tahap 3 dan Tahap 4 TETAP 2 PANGGILAN TERPISAH

**Keputusan: TIDAK ADA 1 fungsi orkestrasi yang otomatis memanggil `build_universe_graph()`
LALU `generate_personas()` secara tersembunyi di dalam `get_or_generate_personas()`.**
Caller (di luar `persona_generator.py`) yang memanggil KEDUA tahap secara eksplisit dan
berurutan.

**Alasan konkret:** `build_universe_graph()` (Tahap 3) punya profil waktu **~18 menit
(universe kecil) sampai ~2,31 JAM (universe besar tanpa filter sektor)** — sudah
didokumentasikan dan DITERIMA sebagai biaya wajar Tahap 3 sendiri
(`docs/design/tahap3_zep_feed_design.md`). Menyembunyikan panggilan sebesar itu DI DALAM
`get_or_generate_personas()` — fungsi yang namanya sendiri menyiratkan "cek cache, kalau
tidak ada baru generate (cepat)" — akan membuat perilaku fungsi itu **radikal berbeda**
tergantung cache-hit (instan) vs cache-miss (bisa BERJAM-JAM) TANPA sinyal apapun di
signature-nya. Kalau ini dipanggil dari HTTP endpoint sinkron, cache-miss untuk universe
besar akan menahan request selama ~2,3 jam — nyaris pasti timeout di sisi mana pun
(load balancer, browser, worker Flask) jauh sebelum selesai. **Memisahkan kedua tahap
secara eksplisit membuat biaya besar itu TERLIHAT di level pemanggilan**, dan membebaskan
caller untuk menjalankan Tahap 3 lewat mekanisme yang sesuai skalanya (job queue,
background worker, CLI/cron terjadwal) — TANPA `persona_generator.py` perlu tahu/peduli
bagaimana Tahap 3 dijalankan.

**Bentuk pemanggilan akhir (ilustratif, di kode CALLER, BUKAN di dalam `persona_generator.py`):**

```python
# Jalur Zep (Tahap 3 + Tahap 4 berurutan, caller yang menyambungkan)
tahap3_result = build_universe_graph(as_of_date=as_of_date, sectors=sectors,
                                      market_cap_tiers=market_cap_tiers)
if not tahap3_result["success"]:
    ...  # tangani kegagalan Tahap 3 sendiri, TIDAK lanjut ke Tahap 4 sama sekali

grounding = build_grounding_from_graph(tahap3_result["filtered_entities"])
grounding.provenance["source_graph_id"] = tahap3_result.get("graph_id")  # lihat catatan di bawah

result = get_or_generate_personas(
    grounding, as_of_date=as_of_date, sectors=sectors, market_cap_tiers=market_cap_tiers,
)
```

```python
# Jalur news (existing, TIDAK berubah cara panggilnya dari sudut pandang caller lama --
# hanya isi generate_personas yang sekarang generik)
universe = screen_universe(sectors=sectors, market_cap_tiers=market_cap_tiers, as_of_date=as_of_date)
grounding = build_grounding_from_news(universe, as_of_date)
result = get_or_generate_personas(
    grounding, as_of_date=as_of_date, sectors=sectors, market_cap_tiers=market_cap_tiers,
)
```

**Temuan lintas-tahap yang HARUS dicatat (bukan diputuskan/diimplementasikan di sini,
prasyarat untuk implementasi):** baris `grounding.provenance["source_graph_id"] = ...` di
atas mengasumsikan `build_universe_graph()` mengembalikan `graph_id` di hasil sukses-nya.
**Dikonfirmasi dari pembacaan ulang `universe_graph_builder.py`: kontrak sukses Tahap 3
SAAT INI TIDAK menyertakan `graph_id` sama sekali** (field-nya: `success`,
`filtered_entities`, `item_count`, `failed_tickers`, `ontology_used`, `ingest_seconds`,
`as_of_date` — 7 field, `graph_id` tidak ada, sesuai desain Tahap 3 §7 yang memang sengaja
tidak menyertakannya karena graph sudah dihapus). **Implementasi field `source_graph_id`
(§5 di bawah) BUTUH perubahan aditif kecil di Tahap 3** (menambah 1 key `graph_id` ke dict
sukses — string historis murni untuk audit, BUKAN referensi graph yang masih hidup, jadi
TIDAK bertentangan dengan siklus hidup create+delete per run yang sudah diputuskan) —
**ini dependency lintas-dokumen yang disurfacekan di sini, bukan diimplementasikan** (di
luar file yang boleh disentuh investigasi/desain ini).

**Convenience wrapper (opsional, HANYA untuk konteks yang memang boleh blocking lama —
skrip CLI/batch job, BUKAN endpoint HTTP sinkron):**

```python
def generate_personas_from_universe_graph(
    as_of_date: Optional[str] = None,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]:
    """Convenience murni: rantai Tahap 3 -> Tahap 4 dalam 1 panggilan BLOCKING.
    JANGAN dipakai dari jalur request HTTP sinkron -- lihat alasan pemisahan di atas.
    Cocok untuk skrip CLI/cron/batch job yang memang menerima blocking lama."""
    tahap3_result = build_universe_graph(as_of_date, sectors, market_cap_tiers)
    if not tahap3_result["success"]:
        return {"success": False, "error": f"Tahap 3 gagal: {tahap3_result['error']}",
                "as_of_date": as_of_date}
    grounding = build_grounding_from_graph(tahap3_result["filtered_entities"])
    grounding.provenance["source_graph_id"] = tahap3_result.get("graph_id")  # lihat catatan
    return get_or_generate_personas(grounding, as_of_date=as_of_date, sectors=sectors,
                                     market_cap_tiers=market_cap_tiers,
                                     force_regenerate=force_regenerate)
```

---

## 5. Bentuk dict hasil — lengkap, field lama TIDAK dihapus, field baru additive

**Field lama yang tidak relevan untuk jalur Zep (`article_ids`, `sample_articles`) —
diisi list kosong `[]`, BUKAN dihilangkan dari dict.** Ini KEBALIKAN dari pola Tahap 3 §7
("`filtered_entities` tidak ada sama sekali di hasil GAGAL") — dan itu SENGAJA beda,
alasannya konteksnya beda: pola Tahap 3 §7 dipakai untuk **membedakan sukses vs gagal**
(field hilang = sinyal "operasi ini gagal total, jangan proses apapun"). Di sini KEDUA
jalur (`news` maupun `zep_graph`) SAMA-SAMA hasil SUKSES yang valid — bedanya cuma
metodologi grounding. Menghilangkan `article_ids` dari hasil jalur `zep_graph` akan
memaksa SETIAP konsumen hasil (`persona_oasis_adapter.py` dan lainnya) menulis
`result.get("article_ids", [])` di setiap tempat yang mengaksesnya, padahal
`persona_oasis_adapter.py` (dikonfirmasi investigasi §5, hanya baca 9 field
`InvestorPersona`) **tidak pernah membaca `article_ids` sama sekali** — jadi tidak ada
konsumen yang butuh field itu ada/hilang secara bermakna; list kosong sudah representasi
paling sederhana dan aman ("tidak ada artikel yang relevan di run ini", true untuk jalur
zep_graph). Field BARU (`source_entity_count` dkk) demikian pula diisi nilai netral
(`0`/`[]`) untuk jalur `news` — bukan dihilangkan — supaya SATU bentuk dict konsisten
dipakai kedua jalur (aman untuk pemrosesan generik, mis. logging/dashboard yang menampilkan
semua field tanpa perlu tahu jalurnya).

**Bentuk lengkap (`generate_personas`/`get_or_generate_personas`, jalur `zep_graph`,
CONTOH KONKRET pakai data nyata investigasi):**

```json
{
  "success": true,
  "run_id": "a1b2c3d4e5f6...",
  "as_of_date": null,
  "screening": {"sectors": ["Communication Services", "Energy"], "market_cap_tiers": null, "grounding_source": "zep_graph"},
  "universe_key": "9f8e7d6c5b4a3210",
  "generated_at": "2026-09-23T14:32:10.123456+00:00",
  "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
  "temperature_used": 1.0,
  "attempt": 1,
  "grounding": "zep_graph",
  "grounding_source": "zep_graph",
  "article_ids": [],
  "sample_articles": [],
  "source_graph_id": "mirofish_universe_live_a1b2c3d4",
  "source_entity_count": 152,
  "source_entity_names": ["Ari Emanuel", "Verizon", "Paramount", "SLB", "DVN", "AT&T", "Mark Shapiro", "Netflix", "Google", "Live Nation Entertainment, Inc.", "..."],
  "source_grounding_tokens": 14812,
  "filter_method_used": "top_degree_token_budget",
  "personas": [
    {
      "name": "Marguerite Voss",
      "tagline": "...",
      "investment_philosophy": "...",
      "philosophy_label": "...",
      "biases": ["...", "..."],
      "personality": "...",
      "communication_style": "...",
      "edge_vs_others": "...",
      "short_bio": "..."
    }
    // ... 7 persona lain, 9 field wajib SAMA PERSIS, TIDAK ADA field provenance di sini
  ],
  "warnings": [],
  "from_cache": false,
  "persisted": true
}
```

**Catatan angka `source_entity_count=152`/`source_grounding_tokens=14.812`:** ini
**ILUSTRASI, bukan hasil pengukuran ulang persis** — investigasi mengukur total 175 entity
= 18.776 token (SEMUA, tanpa budget), dan `token_budget=15.000` di sini PASTI berhenti
sebelum entity ke-175 untuk dataset spesifik itu. Titik potong PASTINYA (entity ke berapa
persis) tidak dihitung ulang di sini karena data mentah 175-entity investigasi sudah tidak
tersimpan (graph Zep sudah dihapus sesuai desain Tahap 3, dan file scratch investigasi juga
sudah dihapus sesuai konvensi). Angka `152`/`14.812` di atas adalah estimasi proporsional
yang masuk akal (≈87% dari 175 entity, konsisten dengan pengamatan bahwa entity berdegree
tinggi—yang dikonsumsi lebih dulu—rata-rata lebih mahal per-token daripada entity ekor
berdegree-1, sehingga persentase ENTITY yang muat sedikit lebih tinggi dari rasio token
mentahnya 15.000/18.776≈79,9%) — bukan klaim presisi ulang.

**Ilustrasi algoritma §2 bekerja pada 4 entity NYATA pertama (degree tertinggi dari data
investigasi, dihitung ULANG dengan tokenizer asli untuk dokumen ini):**

```
entity 1 (Ari Emanuel, degree=10, Person): +218 token, total 218/15.000
entity 2 (Verizon,     degree=10, Company): +387 token, total 605/15.000
entity 3 (Netflix,     degree=6,  Company): +170 token, total 775/15.000
entity 4 (Similarweb,  degree=1,  Company): +61 token,  total 836/15.000
... (berlanjut sampai entity berikutnya akan melebihi 15.000, lalu STOP)
```

(Angka ini persis dari isi entity nyata yang sama seperti dikutip di
`tahap4_persona_from_graph.md` §1 — format teks & tokenizer identik dengan yang dipakai
mengukur 18.776 token total di investigasi, jadi bisa dibandingkan apples-to-apples.)

---

## 6. Persistence — kolom BARU, bukan reinterpretasi `sample_json`

**Kolom BARU: `provenance_json` (TEXT, nullable).** BUKAN mendaur ulang `sample_json`.

**Alasan:** `sample_json` sudah mengikat semantik SPESIFIK (list of `{article_id, ticker,
headline, publisher, published_at}}` — bentuk artikel Finnhub). Menjejalkan
`source_entity_names` (list of string polos, tanpa `article_id`/`ticker`/dst.) ke kolom
yang SAMA berarti pembaca kolom itu (kode maupun manusia) harus tahu dulu `grounding_source`
sebelum bisa menafsirkan ISI kolom `sample_json` dengan benar — persis pola yang MEMBUAT
temuan tabrakan cache key (§3) muncul di investigasi (2 makna beda dipaksa masuk 1
representasi). Kolom BARU dengan nama GENERIK (`provenance_json`, bukan
`graph_provenance_json` — supaya tidak terikat ke "graph" doang kalau nanti ada sumber
grounding ketiga di masa depan) menghindari pengulangan pola yang sama.

```python
_SCHEMA tambahan (migrasi, pola PERSIS seperti kolom sample_json/universe_key/screening_json
yang sudah ada di _connect()):
if "provenance_json" not in cols:
    conn.execute("ALTER TABLE persona_runs ADD COLUMN provenance_json TEXT")
```

`provenance_json` diisi `json.dumps({"source_graph_id":..., "source_entity_count":...,
"source_entity_names":..., "source_grounding_tokens":..., "filter_method_used":...})` untuk
jalur `zep_graph`; **`NULL` untuk jalur `news`** (provenance news sudah cukup lewat
`article_ids_json`/`sample_json` yang sudah ada — TIDAK perlu diduplikasi ke kolom baru ini,
konsisten filosofi "jangan simpan 2 kali kalau 1 kolom sudah eksplisit menjawabnya"). Baris
LAMA (sebelum kolom ini ada) dibaca sebagai `provenance_json=NULL` → adapter/pembaca
memperlakukan sebagai "tidak ada provenance graph" (setara list/dict kosong saat dipakai),
PERSIS pola fallback yang sudah dipakai `sample_json` untuk baris pra-kolom itu
(`json.loads(row[9]) if row[9] else []`, `_load_latest_run` baris 663).

---

## Diagram alur

```
screen_universe(sectors, market_cap_tiers, as_of_date)         [Tahap 1]
        |
        v  (jalur Zep)                              (jalur news, TIDAK berubah)
build_universe_graph(as_of_date, sectors, ...)       screen_universe(...) -> universe
   [Tahap 3, ~18min-2,31 jam, TERPISAH dari Tahap 4]         |
        |                                                     v
        +--- gagal ---> tangani di caller,           build_grounding_from_news(universe, as_of_date)
        |               TIDAK lanjut ke Tahap 4                |
        v                                                      |
   filtered_entities                                            |
        |                                                       |
        v                                                       |
build_grounding_from_graph(filtered_entities,                   |
                            token_budget=15000)   [BARU, §2]     |
   - sort by degree desc                                        |
   - isi sampai budget token (tokenizer nyata)                  |
        |                                                       |
        v                                                       v
   GroundingResult(news_lines, grounding, source="zep_graph", ...)  atau  source="news"
        |                                                       |
        +-------------------------+----------------------------+
                                   v
              generate_personas(grounding, as_of_date, sectors, ...)  [REUSE 100% loop, §1]
                 - _build_persona_prompt(grounding.news_lines, grounding.grounding)  [TIDAK BERUBAH]
                 - loop retry/temperature/validasi 8-nama-unik                        [TIDAK BERUBAH]
                 - hasil + provenance dari grounding.provenance + grounding.source
                        |
                        v
              get_or_generate_personas(grounding, ...)   [persistence, cache key §3 diperbaiki]
                 - _screening_params(..., grounding_source=grounding.source)
                 - universe_key = sha256(screening)  -- "news" backward-compatible,
                                                          "zep_graph" selalu key baru
                 - _save_run(...) -> persona_runs (+ kolom provenance_json BARU, §6)
                        |
                        v
                 8× InvestorPersona (9 field wajib, TIDAK BERUBAH)
                        |
                        v
              -----> Tahap 5 (persona_oasis_adapter.py, TIDAK PERLU disentuh)
```

---

## Ringkasan signature (untuk implementasi nanti)

```python
@dataclass
class GroundingResult:
    news_lines: List[str]
    grounding: str            # "news" | "zep_graph" | "none"
    source: str               # "news" | "zep_graph"
    provenance: Dict[str, Any]

def build_grounding_from_news(
    universe: List[Dict[str, Any]], as_of_date: Optional[str],
) -> GroundingResult: ...

def build_grounding_from_graph(
    filtered_entities: Dict[str, Any], token_budget: int = ZEP_GROUNDING_TOKEN_BUDGET,
) -> GroundingResult: ...

def generate_personas(
    grounding: GroundingResult,
    as_of_date: Optional[str] = None,
    max_attempts: int = 3,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]: ...

def get_or_generate_personas(
    grounding: GroundingResult,
    as_of_date: Optional[str] = None,
    force_regenerate: bool = False,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
) -> Dict[str, Any]: ...

# Convenience, HANYA konteks blocking-boleh (CLI/batch), lihat §4:
def generate_personas_from_universe_graph(
    as_of_date: Optional[str] = None,
    sectors: Optional[List[str]] = None,
    market_cap_tiers: Optional[List[str]] = None,
    force_regenerate: bool = False,
) -> Dict[str, Any]: ...
```

---

## Di luar scope dokumen ini

- **Perubahan Tahap 3** (`universe_graph_builder.py`) untuk menambah `graph_id` ke kontrak
  sukses — dependency yang disurfacekan di §4, BUKAN diimplementasikan/diputuskan di sini
  (di luar file yang boleh disentuh dokumen ini; perlu review terpisah terhadap desain
  Tahap 3 yang sudah final).
- **Titik potong pasti (entity ke berapa) untuk contoh 175-entity di §5** — diberi label
  eksplisit sebagai ESTIMASI, bukan diukur ulang (data mentah investigasi sudah tidak ada).
- **Mekanisme pre-warming tokenizer HuggingFace di deployment** (operasional, dicatat
  sebagai prasyarat di §2, bukan diselesaikan di sini).
- **Peringkasan/ranking LEBIH LANJUT dari top-N-by-degree** (mis. mempertimbangkan
  kedekatan tanggal, sentimen, dsb.) — di luar scope, `top_degree_token_budget` yang
  dipakai sekarang sudah dianggap cukup berdasarkan investigasi §3.
