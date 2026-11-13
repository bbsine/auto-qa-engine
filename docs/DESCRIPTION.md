# AutoQA API Test Üretici (`auto-qa-engine`)

**OpenAPI 3.0 / 3.1 ve Swagger 2.0 şemalarından bağımlılık sıralı (ODG), AST sözdizimi doğrulamalı Pytest entegrasyon testleri, sınır değer senaryoları ve API güvenlik kontrolleri üreten otonom QA ajanı.**

---

## 1. Ne Yapar?

Geleneksel yapay zekâ araçlarına ham bir Swagger/OpenAPI dosyası verdiğinizde; sözdizimi bozuk Python kodları, var olmayan kütüphaneler ve kaynak henüz oluşturulmadan (`POST /users`) kaynağı okumaya çalışan (`GET /users/{id}`) hatalı test sıralamaları (`404 Not Found`) üretilir.

**AutoQA API Test Üretici (`auto-qa-engine`)**, **"Önce Deterministik Planlayıcı, Sonra Yapay Zekâ" (Planner-First, AI-Second)** mimarisiyle çalışır:
1. **Deterministik OpenAPI & Swagger 2.0 Ayrıştırıcı:** OpenAPI 3.0 / 3.1 (`requestBody`, `components/schemas`) ve Swagger 2.0 (`in: body`, `in: formData`, `definitions`, `responses.<code>.schema`) JSON/YAML şemalarını ve yerel `$ref` / `allOf` bileşenlerini deterministik olarak ayrıştırır. Gövde kodlaması medya türüne göre seçilir: `application/json` için `json=`, `application/x-www-form-urlencoded` için `data=`, `multipart/form-data` için ise form alanları `data=` ile, `type: file` veya `format: binary`/`byte` alanları ise bellek içi örnek dosya (`("sample.txt", b"sample", "text/plain")`) olarak `files=` ile gönderilir.
2. **Operasyon Bağımlılık Grafiği (ODG):** Hangi uç noktanın kaynak kimliği (`id`, `user_id`, `item_id`) ürettiğini ve hangisinin tükettiğini grafik teorisiyle (Kahn topolojik sıralama algoritması) çözer. Testleri `POST (Oluştur)` $\to$ `GET (Doğrula)` $\to$ `PUT/PATCH (Güncelle)` $\to$ `DELETE (Temizle)` sırasıyla birbirine bağlar.
3. **Semantik Veri ve Güvenlik Sentezleyici:** Platform LLM Proxy (`fast` modeli) veya deterministik sentezleyici ile şema kısıtlarına uygun gerçekçi Türkçe/İngilizce pozitif veriler, sınır değer ihlalleri (`400`/`422`), yazma uç noktaları için SQLi/XSS enjeksiyon dizeleri (`*_security_injection`) ve parametreli/kimlik doğrulamalı uç noktalar için bilinmeyen kimlik + geçersiz Bearer token yoklamaları (`*_security_unknown_id_and_auth`) üretir.
4. **AST Sözdizimi Eleği ve Öz-Onarım (Self-Healing):** Üretilen her Pytest dosyası müşteriye dönmeden önce Python `ast.parse()` sözdizimi doğrulamasından ve tehlikeli çağrı (`eval`, `exec`, `subprocess`, `open`) denetiminden geçer. Sözdizimi hatası varsa tek turda onarılır veya deterministik şablona geri düşülerek sözdizimsel olarak geçerli Python kodu üretilir.

---

## 2. Kimler İçin?

- **Backend ve API Geliştiricileri:** Yeni yazdıkları uç noktalar için dakikalar içinde uçtan uca entegrasyon ve sınır değer test paketi oluşturmak isteyen ekipler.
- **QA ve Test Otomasyon Mühendisleri:** Karmaşık mikroservis şemalarında veri bağımlılıklarını elle kurgulamak yerine otomatik `ODG_STATE` zinciriyle uç nokta test kapsamasını sistematik olarak artırmak isteyen uzmanlar.
- **DevSecOps Ekipleri:** GitHub Actions CI hattında her Pull Request'te API test kapsamasını (`analyze_coverage`) denetlemek ve kapsama eşiği altında kalan PR'ları otomatik durdurmak (`status: "fail"`) isteyen organizasyonlar.

---

## 3. Desteklenen Operasyonlar ve Örnek Girdi / Çıktı

### Operasyon 1: `generate_tests`
OpenAPI şemasından ODG planı, çalıştırılabilir Pytest kodu (`test_code`) ve sentetik veri seti (`mock_data`) üretir.

- **Taban Adres (`base_url`) ve Şema Yol Ön Eki Kuralı:** Şemadaki `basePath` (Swagger 2.0) veya `servers[0].url` yol bileşeni (OpenAPI 3, `{scheme}` gibi şablon değişkenleri hariç) yol ön eki olarak saklanır. Kullanıcı `base_url` vermezse şema sunucusu (yoksa `http://localhost:8000`) kullanılır; yalnızca kök/boş yol içeren bir `base_url` verirse (ör. `http://127.0.0.1:8080`) şema ön eki otomatik eklenir (`http://127.0.0.1:8080/v2`); `base_url` zaten bu ön ekle bitiyorsa yinelenmez; başka bir yol içeriyorsa olduğu gibi bırakılır. Nihai adres üzerinde SSRF ve hassas veri kontrolleri yine çalıştırılır.

- **`estimated_coverage_pct` Hesaplama Formülü:**
  $$\text{estimated\_coverage\_pct} = \text{round}\left(100 \times \frac{\text{En az 1 pozitif test üretilen operasyon sayısı}}{\text{target\_endpoints filtresi öncesi OpenAPI şemasındaki toplam operasyon sayısı}}, 2\right)$$
  Tüm şema hedeflendiğinde `%100.0`, `target_endpoints` ile bir alt küme seçildiğinde ise seçilen alt kümenin tüm şemaya oranı döner.

**Örnek Girdi (`input`):**
```json
{
  "base_url": "https://api.ornek-magaza.com",
  "include_negative_tests": true,
  "include_security_tests": true,
  "openapi_spec": "openapi: '3.0.3'\ninfo:\n  title: Sipariş API\n  version: '1.0.0'\npaths:\n  /users:\n    post:\n      operationId: create_user\n      requestBody:\n        required: true\n        content:\n          application/json:\n            schema:\n              type: object\n              required: [name, email]\n              properties:\n                name: {type: string, minLength: 2}\n                email: {type: string, format: email}\n      responses:\n        '201':\n          description: Oluşturuldu\n  /users/{id}:\n    get:\n      operationId: get_user\n      parameters:\n        - name: id\n          in: path\n          required: true\n          schema: {type: string}\n      responses:\n        '200':\n          description: Başarılı"
}
```

**Örnek Çıktı (`output` — gerçek motor çıktısı):**
```json
{
  "summary": {
    "total_endpoints": 2,
    "total_operations": 2,
    "generated_test_count": 5,
    "positive_tests": 2,
    "negative_boundary_tests": 1,
    "security_injection_tests": 2,
    "ast_valid": true,
    "self_healed": false,
    "estimated_coverage_pct": 100.0
  },
  "odg_plan": [
    {
      "step": 1,
      "operation_id": "create_user",
      "method": "POST",
      "path": "/users",
      "produces": ["user_id"],
      "consumes": []
    },
    {
      "step": 2,
      "operation_id": "get_user",
      "method": "GET",
      "path": "/users/{id}",
      "produces": [],
      "consumes": ["user_id"]
    }
  ],
  "endpoints_covered": ["/users", "/users/{id}"],
  "total_assertions": 12,
  "test_code": "import pytest\nimport httpx\n...",
  "mock_data": {
    "create_user": {
      "valid_payload": {"name": "Ayşe Yılmaz", "email": "ayse.yilmaz@ornek.com"},
      "boundary_payloads": [{}, {"name": "", "email": 12345}],
      "security_payloads": [{"name": "'; DROP TABLE users; -- <script>alert(1)</script>", "email": "admin@example.com' OR '1'='1"}]
    }
  }
}
```

### Operasyon 2: `generate_mocks`
Yalnızca sentetik pozitif, sınır değer ve güvenlik test verilerini JSON olarak döndürür.

### Operasyon 3: `analyze_coverage`
OpenAPI şemasını ve mevcut test kodunuzu (`existing_test_code`) AST çağrı düğümleri bazında karşılaştırarak kapsama oranını (`coverage_pct`) hesaplar; `min_coverage_pct` altındaysa CI adımı için `status: "fail"` döndürür.

---

## 4. Yapılandırma Ayarları (`config_schema`)

| Alan Adı | Tip | Varsayılan | Açıklama |
| :--- | :--- | :--- | :--- |
| `test_framework` | `string` (`pytest-httpx` \| `pytest-requests`) | `pytest-httpx` | Üretilecek test kodunun HTTP istemci şablonu. |
| `default_locale` | `string` (`tr-TR` \| `en-US`) | `tr-TR` | Sentetik verilerin (isim, şehir, e-posta, metin) yerel dili. |
| `min_coverage_pct` | `integer` (`0`–`100`) | `80` | `analyze_coverage` operasyonunda CI geçişi için gereken minimum kapsama yüzdesi. |
| `fail_on_missing_auth` | `boolean` | `false` | Kimlik doğrulama şeması olmayan yazma uç noktalarında `needs_review` durumu döndürür. |

---

## 5. Sınırlamalar (Dürüst Beyan — spec_version 1.1 §2.7)

1. **Canlı Hedef Sunucuya Bağlanmaz ve `ast.parse` Yalnızca Sözdizimini Doğrular:** Container tam ağ izolasyonu (`network.allow: []`) ile çalışır ve müşterinin hedef API sunucusuna kendisi HTTP isteği atmaz. `ast.parse()` kontrolü üretilen Pytest kodunun geçerli Python sözdizimine sahip olduğunu garanti eder; testlerin hedef sunucuya karşı çalışma zamanında geçmesi hedef sunucunun şemaya uyumuna bağlıdır.
2. **Sezgisel Sahiplik (BOLA) Kontrolleri:** Pozitif testlerdeki kaynak sahipliği doğrulamaları sezgiseldir; yalnızca JSON yanıtındaki `user_id`, `userId`, `owner_id` veya bağlanan yol parametresi (`param_name` / `state_key`) anahtarlarını kontrol eder. Sahiplik bilgisi farklı veya iç içe özel bir alan adıyla dönüyorsa statik şablon bunu yakalayamaz. Ayrıca `*_security_unknown_id_and_auth` testi iki ayrı kiracı oturumu kurgulamak yerine var olmayan kaynak kimliği ve geçersiz Bearer token ile `400/401/403/404/422` kontrolü yapar.
3. **Üretici Adım Başarısızlığında Bağımlı Testlerin Atlanması (`skipped > 0`):** Bir üretici adım (ör. `POST /users`) çalışma zamanında `500` döner ve `ODG_STATE` içine kaynak kimliği kaydedemezse, bu kimliğe bağımlı tüketici testler sahte kimlikle yanıltıcı sonuç vermemek için `pytest.skip("Producer step did not capture: ...")` ile atlanır. CI hatlarında `skipped > 0` durumu şüpheli/başarısız olarak ele alınmalıdır (`pytest -rfs`).
4. **Hassas Ödeme Verisi Filtresi Yanlış Pozitif Oranı (~%2):** `spec_version 1.1` §4.3 gereği gerçek kredi kartı IIN ön eki (Visa `4`, Mastercard `51-55` vb.) ve Luhn sağlamasını aynı anda geçen 13–19 haneli sayı dizileri `HTTP 422` (`prohibited_sensitive_data`) ile reddedilir. Bu kural, kart ön ekiyle başlayan rastgele 16 haneli sayısal sipariş/takip numaralarının yaklaşık **%2.6**'sında yanlış pozitif ret üretebilir.
5. **LLM Zenginleştirme Zaman Aşımı ve Deterministik Geri Dönüş:** Platform LLM Proxy çağrılarında varsayılan zaman aşımı **12.0 saniyedir** (opsiyonel `LLM_TIMEOUT_S` ortam değişkeni ile `0.1`–`45.0` sn arasında ayarlanabilir). LLM zaman aşımına uğrarsa veya geçersiz yanıt dönerse ajan hata vermek yerine deterministik sentetik verilere geri düşer.
6. **Dış Şema URL'leri (`$ref: "https://..."`) ve Protokol Kapsamı:** Güvenlik ve SSRF koruması gereği dış sunuculardaki uzak `$ref` adresleri indirilmez; yalnızca şema içi (`#/components/schemas/...` ve Swagger 2.0 `#/definitions/...`) referanslar çözümlenir. OpenAPI 3.0 / 3.1 ve Swagger 2.0 (`in: body`, `in: formData`) REST/HTTP uç noktaları desteklenir; GraphQL, gRPC ve WebSocket protokolleri kapsam dışıdır.
