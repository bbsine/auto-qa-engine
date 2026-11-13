# {{AGENT_NAME}} (`{{AGENT_SLUG}}`) — Ortak Entegrasyon Rehberi

Bu belge, **{{AGENT_NAME}}** (`{{AGENT_SLUG}}`) ajanının üç desteklenen platform kanalında (**CI**, **MCP**, **API**) nasıl entegre edileceğini, güvenlik kurallarını ve çalışma sözleşmesini açıklar.

---

## 1. Hangi Kanalı Seçmelisiniz?

| Entegrasyon Kanalı | Ne Zaman Kullanılır? | Nasıl Çalışır? |
| :--- | :--- | :--- |
| **CI (GitHub Actions)** | Her `push` veya `Pull Request` sırasında API sözleşmesini otomatik test etmek ve kapsama eşiği (`min_coverage_pct`) altında kalan PR'ları durdurmak için. | `.github/workflows/ci.yml` içinde `platform-tld/agent-action@v1` adımı olarak çalışır. `status: "fail"` dönerse CI adımı başarısız sayılır. |
| **MCP (Model Context Protocol)** | Claude Code, Cursor veya Windsurf içinde kod yazarken mevcut OpenAPI şemasından etkileşimli olarak `pytest` dosyası ve mock veri üretmek için. | AI kodlama aracına MCP sunucusu (`https://mcp.platform.tld/{{AGENT_SLUG}}`) olarak eklenir. |
| **API (SDK / REST)** | Kendi iç QA otomasyon hattınızdan veya arka uç (backend) servisinizden programatik olarak test paketi ve sentetik veri üretmek için. | `@platform/sdk` veya `POST https://api.platform.tld/v1/agents/{{AGENT_SLUG}}/invoke` uç noktası ile çağrılır. |

---

## 2. Desteklenen Operasyonlar (`{{OPERATIONS}}`)

1. **`generate_tests`**:
   - **Girdi:** `openapi_spec` (JSON/YAML metni), `base_url`, `include_negative_tests`, `include_security_tests`, `target_endpoints`.
   - **Çıktı:** Topolojik sıralı `odg_plan`, AST doğrulamasından geçmiş çalıştırılabilir `test_code` (`pytest_code`), `mock_data` (`mock_datasets`), `endpoints_covered`, `total_assertions` ve `test_generated_api.py` artefaktı.
2. **`generate_mocks`**:
   - **Girdi:** `openapi_spec`, `count`.
   - **Çıktı:** Her operasyon için gerçekçi pozitif (`valid_payload`), sınır değer (`boundary_payloads`) ve OWASP güvenlik (`security_payloads`) veri setleri.
3. **`analyze_coverage`**:
   - **Girdi:** `openapi_spec`, `existing_test_code`.
   - **Çıktı:** `coverage_pct`, kapsanan/kapsanmayan operasyon listesi ve güvenlik önerileri. Kapsama oranı `config.min_coverage_pct` (varsayılan `%80`) altındaysa `status: "fail"` döner.

---

## 3. Anahtar ve Güvenlik Kuralları

1. **Sunucu ve CI Gizliliği:**
   - Platform gizli anahtarı yalnızca sunucu tarafında (`process.env.PLATFORM_SECRET_KEY` veya `os.environ["PLATFORM_SECRET_KEY"]`) ya da GitHub Actions Secrets (`${{ secrets.PLATFORM_SECRET_KEY }}`) içinde tutulur.
   - Gizli anahtar asla istemci (frontend) koduna, kaynak kod deposuna (git repo) veya AI aracının sohbet geçmişine yazılmaz.
2. **`.env` ve `.gitignore` Zorunluluğu:**
   - Yerel geliştirmede `.env` dosyasına yalnızca boş `PLATFORM_SECRET_KEY=` satırı eklenir; gerçek değer geliştirici tarafından platform panelinden kopyalanarak yerel `.env` dosyasına yapıştırılır.
   - `.env` dosyasının `.gitignore` içinde yer aldığı her kurulumda doğrulanmalıdır.
3. **Ağ ve Veri Güvenliği:**
   - Ajan dış ağa kapalıdır (`network.allow: []`); OpenAPI şemasını yerel URL yerine doğrudan JSON/YAML metni olarak gönderin.
   - Girdi şemalarında gerçek kredi kartı numarası, CVV veya gerçek kişisel veri (KVKK) bulundurmayın; ajan yalnızca sentetik test verisi üretir.

---

## 4. Doğrulama Deneyimi Beyanı (spec_version 1.1 §3.3 Kural 6)

> **TODO (Geliştirici / Proje Sahibi Tarafından Doldurulacak):**
> Rehber Bölüm 3.3 Kural 6 uyarınca bu klasördeki entegrasyon talimatları teslimden önce **en az iki farklı gerçek proje üzerinde** bizzat denenmeli ve sonuçları aşağıdaki tabloya işlenmelidir. (Otomatik denetim / AI ajanı bu adımı geliştirici adına uydurmaz; gerçek deneme sonrasında tabloyu doldurun.)

| Proje Türü / Çatısı | Kullanılan Araç (Claude Code / Codex / Cursor) | Tarih | Sonuç / Gözlemler |
| :--- | :--- | :--- | :--- |
| _TODO: Örn. Python / FastAPI projesi_ | _TODO_ | _TODO_ | _TODO_ |
| _TODO: Örn. Node.js / Express + OpenAPI projesi_ | _TODO_ | _TODO_ | _TODO_ |

