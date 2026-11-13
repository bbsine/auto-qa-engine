Bu projeye "{{AGENT_NAME}}" (`{{AGENT_SLUG}}`) ajanını entegre et. Kullanılabilir operasyonlar: {{OPERATIONS}}

Lütfen aşağıdaki adımları sırasıyla uygula:

1. **Projeyi ve OpenAPI Kaynağını Tespit Et:**
   - Projenin dilini ve web çatısını tespit et (Python/FastAPI/Django, Node.js/Express/NestJS, Java/Spring Boot, PHP/Laravel veya Go) ve bana kısaca özetle.
   - Projedeki mevcut OpenAPI/Swagger şema dosyasını (`openapi.yaml`, `openapi.json`, `swagger.json`) veya şema üreten uç noktayı (ör. FastAPI `app.openapi()`) bul. Şema dosyası yoksa mevcut rotalardan temel bir `openapi.yaml` oluştur.
   - Üretilecek Pytest entegrasyon testlerinin yerleşeceği dizini belirle (varsayılan: `tests/integration/test_generated_api.py`).

2. **Doğru Entegrasyon Kanalını Seç ve Kur:**
   - **Kanal A — CI Kalite Kapısı (Her PR ve Push için önerilen):** `.github/workflows/auto-qa.yml` dosyası oluştur ve `platform-tld/agent-action@v1` aksiyonunu kullanarak hem `analyze_coverage` hem de `generate_tests` operasyonlarını bağla (`fail-on: fail` ayarıyla kapsama eşiği altında kalan PR'ları durdur).
   - **Kanal B — MCP Sunucusu (Etkileşimli geliştirme için):** Geliştirici Claude Code içinden doğrudan test üretmek istiyorsa aşağıdaki MCP ekleme komutunu hazırla:
     ```bash
     claude mcp add --transport http {{AGENT_SLUG}} https://mcp.platform.tld/{{AGENT_SLUG}} \
       --header "Authorization: Bearer $PLATFORM_SECRET_KEY"
     ```
   - **Kanal C — Sunucu Tarafı API / Betik Çağrısı:** Projeye `scripts/run_auto_qa.py` (veya Node projelerinde `scripts/run-auto-qa.mjs`) yardımcı betiği ekle; bu betik OpenAPI şemasını okuyup `POST https://api.platform.tld/v1/agents/{{AGENT_SLUG}}/invoke` uç noktasına `generate_tests` isteği atsın.

3. **Anahtar Güvenliğini Sağla (`.env` + `.gitignore`):**
   - Sunucu ve betik tarafında kimlik doğrulama anahtarını yalnızca `PLATFORM_SECRET_KEY` ortam değişkeninden oku.
   - Proje kökündeki `.env` dosyasına `PLATFORM_SECRET_KEY=` satırını **BOŞ** olarak ekle (değeri ben panelden kopyalayıp kendim yazacağım).
   - `.env` dosyasının `.gitignore` içinde bulunduğunu kontrol et; yoksa `.gitignore` dosyasına `.env` satırını ekle. Anahtarı asla istemci koduna, repoya veya sohbet geçmişine yazma.

4. **{{AGENT_NAME}} Ajanına Özel Çıktı İşleme Adımlarını Ekle:**
   - `generate_tests` yanıtındaki `output.odg_plan` adımlarını (Producer $\to$ Consumer sıralamasını) konsola özet tablo olarak yazdır.
   - Yanıttaki `output.test_code` (veya `artifacts[0].content`) içeriğini `tests/integration/test_generated_api.py` dosyasına kaydet.
   - Yanıttaki `output.mock_data` içeriğini `tests/integration/fixtures_mock_data.json` olarak kaydet.
   - Agent çağrılarında 60 saniye zaman aşımı (`timeout`) tanımla; bağlantı hatası durumunda mevcut yerel `pytest` testlerini çalıştırmaya devam eden güvenli bir geri dönüş (fallback) uygula.

5. **Doğrulama Adımı:**
   - `{{SAMPLE_INPUT}}` örnek girdisini kullanarak entegrasyon betiğini veya testini çalıştır (`PLATFORM_SECRET_KEY` tanımlı değilse testi atlayıp uyarı ver).
   - Yanıtta `status: "ok"`, `output.summary.ast_valid: true` ve `output.test_code` alanlarının döndüğünü doğrula.

6. **Özet:**
   - Oluşturduğun ve değiştirdiğin tüm dosyaları liste hâlinde bana özetle.
