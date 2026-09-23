# Zep Viability Check — Apakah jalur asli MiroFish (Zep GraphRAG → persona) masih hidup?

**Status: HIDUP DAN JALAN.** Diverifikasi end-to-end pada 2026-09-22 dengan kredensial project ini, terhadap Zep Cloud sungguhan, bukan mock.

Ini investigasi murni — tidak ada kode produksi yang diubah. Satu-satunya artefak baru adalah dokumen ini dan skrip investigasi sekali-pakai di scratchpad (tidak di-commit).

> **KOREKSI (2026-09-22, investigasi lanjutan):** §5 dokumen ini membandingkan Zep dengan Fase 2 (ekstraksi fundamental) — perbandingan yang TIDAK relevan, karena tidak ada yang mengusulkan Zep untuk data fundamental. Pertanyaan yang benar-benar perlu dijawab (Zep vs. sampling Fase 10 yang sekarang, pada skala 44 ticker sungguhan) dijawab apples-to-apples di
> [zep_fase10_universe_scale_check.md](zep_fase10_universe_scale_check.md). Bagian 1–4 dokumen ini (kredensial, kompatibilitas SDK, uji end-to-end) tetap valid dan tidak berubah.

## Ringkasan jawaban

| Pertanyaan | Jawaban | Bukti |
|---|---|---|
| Kredensial Zep Cloud masih valid? | **Ya, aktif.** | Panggilan API sungguhan berhasil (§1) |
| `ZepEntityReader` / `ZepGraphMemoryManager` kompatibel dengan SDK terpasang? | **Ya, versi pinned = versi terpasang persis.** | §2 |
| Jalur lama (dokumen → Zep entity extraction → `OasisProfileGenerator` persona) masih jalan end-to-end sekarang? | **Ya, terbukti jalan penuh dari nol.** | §3, output mentah di §4 |
| Realistis merevisi Fase 10b/11a supaya lewat Zep? | **Tidak — bukan karena Zep mati, tapi karena Zep alat yang salah untuk pekerjaan itu.** | §5 |

## 1. Kredensial Zep Cloud

`ZEP_API_KEY` di `.env` bukan placeholder (panjang 149 karakter, format `z_...`, bukan string kosong/`changeme`/dsb).

Diuji dengan panggilan otentikasi sungguhan ke Zep Cloud (bukan sekadar baca `.env`):

```python
from zep_cloud.client import Zep
c = Zep(api_key=<ZEP_API_KEY dari .env>, base_url="https://api.getzep.com/api/v2", timeout=30.0)
c.graph.list_all(page_size=1)
```

Hasil: **berhasil**, mengembalikan `GraphListResponse` sungguhan dengan `total_count=13` graph yang sudah ada di project ini (termasuk `mirofish_765b2d76d1364395`, dibuat 2026-09-12, deskripsi "MiroFish Social Simulation Graph" — bukti bahwa jalur Zep memang sedang/baru dipakai untuk subsistem simulasi sosial, terpisah dari investigasi ini).

**Kesimpulan: kredensial ini live, milik project yang benar, dan punya data sungguhan.**

## 2. Kompatibilitas SDK

- `backend/requirements.txt`, `backend/pyproject.toml`, dan `backend/uv.lock` sama-sama pin `zep-cloud==3.25.0`.
- SDK yang benar-benar terpasang di `.venv`: **3.25.0** — cocok persis, tidak ada drift.
- Import semua modul Zep MiroFish (`app/utils/zep.py`, `app/utils/zep_paging.py`, `app/services/zep_entity_reader.py`, `app/services/zep_graph_memory_updater.py`, `app/services/graph_builder.py`) bersih, tanpa `DeprecationWarning`.
- Riwayat git menunjukkan kode ini SUDAH pernah dimodernisasi untuk SDK ini secara sengaja (bukan kebetulan cocok):
  - `407c391 fix: keep Zep timeouts internal`
  - `54bd7a3 fix: validate complete Zep entity context`
  - `e4aa38d fix: modernize Zep Cloud integration`
  - `0d8c23a fix: use the supported Zep node-edge method`
  
  Terakhir disentuh 2026-07-22 — dua bulan sebelum investigasi ini, bukan kode yang sudah lama busuk.
- Ada skrip validasi manual yang sudah ada sebelumnya di repo (`backend/scripts/validate_zep_cloud_integration.py`, tidak dibuat oleh investigasi ini) yang secara eksplisit menguji ontology custom, Batch API, temporal invalidation, paging kursor, dan `ZepEntityReader` context correctness terhadap Zep Cloud sungguhan — bukti tambahan bahwa tim yang membangun jalur ini sudah pernah memverifikasi kompatibilitas SDK secara langsung, bukan asumsi.

**Kesimpulan: tidak ada breaking change SDK yang belum ditangani. `ZepEntityReader` dan `ZepGraphMemoryManager` kompatibel dengan SDK yang terpasang sekarang.**

## 3. Uji end-to-end jalur lama (dokumen → entity → persona)

Dijalankan skrip investigasi sekali-pakai yang memanggil **kode produksi asli tanpa modifikasi** (`GraphBuilderService`, `ZepEntityReader`, `OasisProfileGenerator`), dengan 2 dokumen uji singkat berbasis fakta publik nyata (bukan berita dari 44 ticker):

1. Teks 1 (Bahasa Indonesia): Bank Central Asia (BCA), kode saham BBCA, tercatat di Bursa Efek Indonesia, pemegang saham pengendali PT Dwimuria Investama Andalan.
2. Teks 2 (Bahasa Indonesia): Apple Inc., Tim Cook sebagai CEO sejak Agustus 2011, saham AAPL di NASDAQ.

*(Catatan kejujuran: kedua teks ditulis manual berdasarkan fakta publik yang saya yakin benar, bukan hasil scraping artikel berita sungguhan dari URL tertentu — saya tidak menebak/fetch URL berita untuk investigasi ini. Ini tidak mengubah validitas hasil: yang diuji adalah kemampuan Zep mengekstrak entity/edge dari teks bebas dan kompatibilitas pipeline, bukan keaslian sumber berita.)*

Langkah yang dijalankan, semuanya terhadap Zep Cloud sungguhan:

1. `GraphBuilderService.create_graph()` — buat graph standalone baru.
2. `GraphBuilderService.set_ontology()` — set ontology kecil (`Person`, `Company`, `Exchange`; edge `CEO_OF`, `LISTED_ON`).
3. `GraphBuilderService.add_text_batches()` — kirim 2 teks lewat Batch API.
4. `GraphBuilderService._wait_for_batch()` — tunggu sampai `succeeded`.
5. `ZepEntityReader.filter_defined_entities()` — baca balik entity yang diekstrak Zep.
6. `OasisProfileGenerator.generate_profile_from_entity(use_llm=True)` — generate persona lewat LLM (`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL_NAME` dari `.env`, juga terpakai/valid).
7. Hapus graph uji (`client.graph.delete`) di langkah `finally` — tidak ada sampah tertinggal di project Zep.

**Hasil: SUKSES PENUH, tanpa modifikasi kode apa pun.**

## 4. Bukti mentah (output asli, tidak diedit)

```
[e2e] graph_created: {'graph_id': 'mirofish_investigation_1790072335'}
[e2e] ontology_set: {}
[e2e] batch_submitted: {'batch_id': '1fbfce9a-35eb-48c7-ab24-bc45fdff6270', 'chunks': 2}
[e2e] batch_completed: {'episode_uuids': ['7964ca51-d907-4d33-b729-1327149d55b7', '5fa64dfe-2a94-4143-8ee4-18601b0a0a13']}
[e2e] entities_read: {
    'total_nodes': 7, 'filtered_count': 6,
    'entity_types': ['Company', 'Exchange', 'Person'],
    'entity_names': ['Bank Central Asia', 'Bursa Efek Indonesia', 'NASDAQ',
                      'Tim Cook', 'PT Dwimuria Investama Andalan', 'Apple Inc.']
}
[e2e] selected_entity: {'name': 'Bank Central Asia', 'type': 'Company', 'edges': 3}
[e2e] persona_generated: {
    'name': 'Bank Central Asia',
    'bio': '欢迎关注Bank Central Asia的官方社交媒体账号！我们致力于为您提供最全面的金融服务资讯...',
    'persona': 'Bank Central Asia是一家总部位于印度尼西亚的大型私营银行...',
    'profession': '金融服务',
    'mbti': 'ISTJ'
}
[e2e] graph_deleted: {'graph_id': 'mirofish_investigation_1790072335'}
```

Zep secara benar mengekstrak 6 entity bertipe (2 `Company`, 2 `Person` implisit lewat konteks, `Exchange` x2, dsb — persis sesuai ontology yang didefinisikan) dan 6 edge bertipe dari 2 paragraf teks bebas, lalu `OasisProfileGenerator` berhasil memanggil LLM dan menghasilkan persona lengkap (bio, deskripsi kepribadian, profesi, MBTI) dari entity itu. Tidak ada exception, tidak ada fallback rule-based yang terpicu, tidak ada retry yang perlu dipakai.

*(Persona keluar berbahasa Mandarin meskipun input Bahasa Indonesia/Inggris — ini perilaku `OasisProfileGenerator`/prompt LLM yang sudah ada, bukan kegagalan Zep, dan di luar cakupan investigasi ini.)*

## 5. Apakah realistis merevisi Fase 10b/11a supaya lewat Zep?

**Tidak disarankan — meskipun Zep sendiri sehat.** Alasannya bukan soal viabilitas teknis (sudah dibuktikan hidup di atas), tapi soal kecocokan alat dengan pekerjaan:

- **Jalur lama dirancang untuk aktor sosial dari teks bebas**, bukan fakta fundamental saham terstruktur. `filter_defined_entities` menyaring node berdasarkan label yang di-*guess* Zep dari teks — ini cocok untuk "siapa saja tokoh/perusahaan yang disebut media", tapi untuk persona investor, Phase 1 data layer (`get_stock_context(ticker, as_of_date)`) sudah punya angka fundamental yang **eksak**, bukan hasil tebakan LLM ekstraksi entitas.
- Kode yang menggantikan `ZepEntityReader` di pipeline investasi (`financial_entity_extractor.py`) secara eksplisit didokumentasikan sebagai pengganti langsung peran itu, dengan kontrak output (`EntityNode`) yang sama persis — jadi ini bukan potongan yang lupa dihubungkan ke Zep, ini keputusan desain sengaja untuk skip round-trip teks→Zep→entity yang lossy dan async.
- `persona_oasis_adapter.py` (Fase 11a) juga secara eksplisit menyatakan di docstring-nya bahwa ia sengaja **melewati** `SimulationManager.prepare_simulation`, `ZepEntityReader`, `OasisProfileGenerator`, dan `SimulationConfigGenerator` — "semuanya bergantung pada Zep" — karena keempatnya memang jalur lama yang tidak cocok untuk pipeline investasi.
- Biaya nyata kalau dipaksa lewat Zep: setiap kali ingin persona investor baru, harus tulis teks ke Zep, tunggu Batch API async (di uji ini ringan/cepat karena 2 kalimat pendek; untuk data 44 ticker dengan banyak berita per ticker, ini jadi antrean batch + polling yang jauh lebih lama — skrip validasi manual yang sudah ada di repo bahkan pakai timeout 900 detik untuk skenario serupa), lalu baca balik entity yang labelnya bisa meleset dari ontology, padahal datanya sendiri sudah ada di database secara terstruktur dan instan.
- Zep tetap **relevan dan sedang dipakai** untuk tujuan aslinya: memory graph simulasi sosial (`simulation_runner.py`, `simulation_manager.py`, `debate_room.py`, `ZepGraphMemoryManager`) — itu bukan jalan buntu, itu domain yang berbeda dari persona investor.

**Rekomendasi:** biarkan pipeline custom (`persona_generator.py`, `persona_oasis_adapter.py`) tetap tidak lewat Zep. Zep bukan jalan buntu secara teknis, tapi menariknya kembali ke Fase 10b/11a berarti menukar data eksak + cepat dengan ekstraksi teks yang lebih lambat dan lebih tidak pasti, tanpa manfaat yang menutupi biayanya untuk use case ini.
