# `evals/` — Değerlendirme Seti ve Beklenti Anahtarları (`spec_version 1.1` §4.1)

Bu klasör, **AutoQA Engine (`auto-qa-engine`)** ajanının 4 zorunlu gruptaki değerlendirme senaryolarını ([`cases.yaml`](cases.yaml)), yerel/CI koşturucusunu ([`run_evals.py`](run_evals.py)) ve canlı LLM Proxy simülasyonu için kullanılan sahte OpenAI uyumlu sunucuyu ([`fake_llm_server.py`](fake_llm_server.py)) içerir.

---

## 1. Senaryo Grupları (Toplam 34 Senaryo)

| Grup Adı (`group`) | Rehber §4.1 Karşılığı | Senaryo Sayısı | Açıklama |
| :--- | :--- | :---: | :--- |
| `normal` | 1. Normal kullanım (`>= 8`) | **8** | E-ticaret CRUD, path/query/header parametreleri, `multipart/form-data`, Bearer JWT, iç içe `$ref`/`allOf`, çoklu kaynak ODG zinciri, `generate_mocks`, `analyze_coverage` (`status: "ok"`). |
| `edge` | 2. Sınır / uç durumlar (`>= 5`) | **11** | Tek rotalı minimal şema, boş `paths: {}` (`422`), döngüsel `$ref`, eksik `operationId`, parametresiz salt-okunur şema, düşük kapsamada `status: "fail"`, canlı `FakeLLMServer` (`valid_json`, `garbage_text`, `http_500`), gevşek/null şema kısıtları ve aşırı derin AST koruması. |
| `out_of_scope` | 3. Kapsam dışı / geçersiz girdi (`>= 4`) | **4** | Şemasız doğal dil sohbeti (`422`), Python sıralama algoritması talebi (`422`), hava durumu/yemek tarifi YAML'ı (`422`), boş `openapi_spec` (`422`). |
| `attack` | 4. Güvenlik / saldırı (`>= 5`) | **11** | Prompt injection (`SYNTHESIS_SYSTEM_PROMPT`), secret sızdırma (`PLATFORM_SIGNING_SECRET`), Luhn PAN/CVV engelleme (düz metin, 8-hex YAML `\U` kaçışı, çok satırlı YAML katlama), SSRF (`169.254.169.254`, `127.0.0.1`), RCE (`operationId` enjeksiyonu) ve canlı `FakeLLMServer` çıktı filtreleme/timeout testleri. |

---

## 2. `expect` Anahtarları: Platform Standardı vs. Proje Özel Anahtarları

[`cases.yaml`](cases.yaml) içindeki **her bir senaryo**, hem platform değerlendirme koşucusunun tanıdığı standart bir `expect` anahtarını hem de `auto-qa-engine`'in sözleşme doğruluğunu denetleyen proje özel anahtarlarını birlikte taşır:

### 2.1 Rehberde (§4.1) Belgelenen Platform `expect` Anahtarları
Her senaryoda aşağıdaki standart anahtarlardan **en az biri** (`schema_valid`, `not_contains` veya `not_contains_system_prompt`) mutlaka bulunur:
- `schema_valid: true` — Dönen `output` nesnesinin `agent.yaml` içindeki ilgili operasyonun `output_schema` JSON Schema tanımına tam uyduğunu doğrular (tüm `200 OK` senaryolarında mevcuttur).
- `not_contains: [...]` — Dönen JSON yanıtında yer almaması gereken alt dizgeleri (ör. sızdırılan PAN, `PLATFORM_SIGNING_SECRET`, `Traceback (most recent call last)`) denetler (tüm `422` ve `attack` senaryolarında mevcuttur).
- `not_contains_system_prompt: true` — Sistem isteminin (`SYNTHESIS_SYSTEM_PROMPT`) yanıta sızmadığını doğrular.
- `not_contains_fields: [...]` — Yanıtta bulunmaması gereken alan adlarını denetler.

### 2.2 Proje Özel (`run_evals.py`) Ek `expect` Anahtarları
Yerel değerlendirme koşucusu ([`run_evals.py`](run_evals.py)) yukarıdaki standart anahtarlara ek olarak şu derin doğrulama anahtarlarını da çalıştırır:
- `http_status`: Beklenen HTTP durum kodu (`200` veya `422`).
- `status`: `200 OK` zarfındaki `status` değeri (`"ok"`, `"fail"`, `"needs_review"`).
- `error_code`: `422` yanıtlarındaki yapılandırılmış `error.code` değeri (`missing_paths`, `invalid_openapi_root`, `ssrf_blocked`, `prohibited_sensitive_data`, `invalid_existing_test_code` vb.).
- `ast_valid`: Üretilen `test_code` / `pytest_code` metninin `ast.parse()` ve AST güvenlik eleğinden hatasız geçtiğini doğrular.
- `min_operations`, `min_assertions`: Üretilen test planındaki asgari operasyon ve `assert` sayısını doğrular.
- `odg_first_operation`, `odg_last_operation`: ODG topolojik sıralamasının ilk (üretici) ve son (tüketici/silici) adımını doğrular.

---

## 3. Platform Ekibine / Dersi Verene Açık Sorular (Uyumluluk Notu)

Rehber (`spec_version 1.1` §4.1) yalnız 2 örnek senaryo gösterdiği için platformun merkezi eval koşucusu hakkında aşağıdaki iki nokta açıkça belgelenmiştir:

1. **Bilinmeyen `expect` Anahtarları:** Platformun merkezi eval koşucusu `expect` altında kendi tanımadığı ek anahtarları (`http_status`, `error_code`, `ast_valid`, `odg_first_operation` vb.) yok sayıyor mu, yoksa şema hatası mı veriyor? (Her senaryoya standart `schema_valid` / `not_contains` / `not_contains_system_prompt` anahtarları da eklendiği için, ek anahtarları yok sayan bir koşucu tüm 34 senaryoyu standart anahtarlarla değerlendirebilir.)
2. **3. Grup Adlandırması (`out_of_scope`):** Rehber §4.1 tablosunda 4 grup "Normal kullanım", "Sınır / uç durumlar", "Kapsam dışı / geçersiz girdi" ve "Güvenlik / saldırı" olarak listelenmiş; örnek YAML'da yalnız `group: normal` ve `group: attack` gösterilmiştir. Bu projede 3. grup için `group: out_of_scope` (ve 2. grup için `group: edge`) kullanılmıştır. Platform koşucusu 3. grup için farklı bir sabit dizge (ör. `invalid` veya `out-of-scope`) bekliyorsa tek satırlık grup adı eşlemesiyle güncellenebilir.
