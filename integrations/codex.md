# {{AGENT_NAME}} (`{{AGENT_SLUG}}`) — Codex Entegrasyon Talimatı

## Bölüm 1: `AGENTS.md` Dosyasına Eklenecek Kalıcı Kurallar

Aşağıdaki bölümü projenizin kök dizinindeki `AGENTS.md` dosyasına ekleyin:

```markdown
## {{AGENT_NAME}} ({{AGENT_SLUG}}) Entegrasyon Kuralları

- **Amaç ve Operasyonlar:** Bu proje, OpenAPI v3.x şemalarından ODG (Operation Dependency Graph) sıralı Pytest entegrasyon testleri ve sentetik test verileri üretmek için `{{AGENT_SLUG}}` ajanını kullanır. Desteklenen operasyonlar: {{OPERATIONS}}
- **Kanal Seçimi:**
  - Pull Request ve Push kontrollerinde `.github/workflows/auto-qa.yml` içindeki `platform-tld/agent-action@v1` (`fail-on: fail`) CI adımı kullanılır.
  - Etkileşimli ajan görevlerinde MCP sunucusu (`https://mcp.platform.tld/{{AGENT_SLUG}}`, `Authorization: Bearer $PLATFORM_SECRET_KEY`) kullanılır.
  - Yerel test üretiminde `POST https://api.platform.tld/v1/agents/{{AGENT_SLUG}}/invoke` uç noktası yalnızca sunucu/CLI betiğinden çağrılır.
- **Anahtar Güvenliği:**
  - Gizli anahtar yalnızca `PLATFORM_SECRET_KEY` ortam değişkeninden (`os.environ["PLATFORM_SECRET_KEY"]` veya `process.env.PLATFORM_SECRET_KEY`) okunur.
  - Gizli anahtarlar asla istemci koduna, kaynak kod deposuna, test dosyalarına veya loglara yazılmaz.
  - `.env` dosyasında yalnızca boş `PLATFORM_SECRET_KEY=` tanımı yer alır ve `.env` mutlaka `.gitignore` içinde tutulur.
- **Çıktı ve Fallback Kuralları:**
  - Projedeki dil/çerçeve (`pyproject.toml`/`requirements.txt` veya `package.json`) ve OpenAPI şeması (`openapi.yaml`/`swagger.json`) tespit edilir; `generate_tests` çıktısı `tests/integration/test_generated_api.py` (Python betiği `scripts/run_auto_qa.py`, Node.js betiği `scripts/run-auto-qa.mjs`) dosyasına yazılır.
  - Ajan çağrılarında zaman aşımı 60 saniyedir (`generate_tests` için `timeout_s: 60`); ajan erişilemezse mevcut yerel test paketi bloklanmadan çalıştırılır.
```

---

## Bölüm 2: Codex Görev Başlatma Prompt'u

```text
AGENTS.md dosyasındaki "{{AGENT_NAME}} ({{AGENT_SLUG}}) Entegrasyon Kuralları" bölümüne uyarak:
1. Projedeki OpenAPI şemasını (openapi.yaml / openapi.json) tespit et.
2. .env dosyasına boş PLATFORM_SECRET_KEY= satırını ekle ve .env dosyasının .gitignore içinde olduğunu doğrula.
3. OpenAPI şemasını {{AGENT_SLUG}} ajanının generate_tests operasyonuna gönderip dönen Pytest kodunu tests/integration/test_generated_api.py dosyasına kaydeden CLI betiğini ve .github/workflows/auto-qa.yml CI iş akışını oluştur.
4. {{SAMPLE_INPUT}} girdisiyle doğrulama testini çalıştır ve değiştirdiğin dosyaları özetle.
```
