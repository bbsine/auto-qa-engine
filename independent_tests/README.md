# Bağımsız Test Paketi (`independent_tests/`)

Bu klasör, projenin kendi `tests/` ve `evals/` dosyalarından **bağımsız** yazılmış kara kutu testlerini içerir.
Testler yalnızca `contract_rules.md` ve `agent.yaml`'a dayanır; `src/` içindeki varsayımları yeniden kullanmaz.

Amaç: projeyi yazan aynı AI'nın kendi kodunu doğrulayan testler yazması riskine (döngüsel doğrulama) karşı
ikinci bir görüş sağlamak.

## Çalıştırma

```bash
# proje kökünden
python -m pytest independent_tests -v
```

Gereksinimler: `requirements.txt` içindekiler yeterli (`fastapi`, `uvicorn`, `httpx`, `pytest`, `pydantic`, `pyyaml`, `openai`).
Ağ gerekmez; uçtan uca testler boş bir yerel porta küçük referans sunucular (`servers.py`) başlatır.

## Neleri doğrular

| Bölüm | İçerik |
| :--- | :--- |
| 1. HMAC / replay | 299 sn geçer, 302 sn reddedilir, yanlış secret, imzadan sonra değişen body, ASCII dışı imza (401, 500 değil) |
| 2. Bozuk girdi | Sayısal kısıtların string/null gelmesi, 3.1 tip listeleri, derin iç içe şema, garip zarflar, çok derin AST: hiçbiri 500 vermez |
| 3. SSRF / `base_url` | Metadata, özel ağ, `file://` engellenir; `localhost`, `127.0.0.1`, `::1`, `::ffff:127.0.0.1` serbest |
| 4. Hassas veri | PAN, CVV, son kullanma, TCKN; YAML `\u`/`\U` kaçışı, satır katlama, anchor ile gizlenenler dahil. Meşru IBAN ve sipariş no geçer |
| 5. Kod enjeksiyonu | Spec `path`'inden üretilen test dosyasında çalıştırılabilir tehlikeli çağrı olmamalı |
| 6. Kapsama analizi | Çağrı bazlı eşleme, yorum satırı ve alt dize adları sayılmaz, f-string yolları anlaşılır |
| 7. Uçtan uca | Üretilen testler gerçek sunuculara karşı koşturulur: doğru sunucuda geçer; başka kullanıcının kaydını dönen (şemalı ve şemasız), zorunlu alanı eksik dönen sunucuda düşer; üretici düşünce bağımlı testler `skip` olur |
| 8. LLM yolu | Sahte OpenAI uyumlu sunucuyla: geçerli zenginleştirme kullanılır; PAN/TCKN/sistem promptu/secret sızıntısı, bozuk metin ve HTTP 500 durumlarında deterministik veriye düşülür; timeout ayarı çalışır |

## Doğrulama durumu

- Proje kodunun 8 Kasım 2026 tarihli sürümünün sandbox kopyasında **102 test geçti**.
- Aynı paket projenin önceki kod sürümlerine karşı çalıştırıldığında 15 ile 39 arasında test başarısız oldu, yani
  paket gerçekten hata yakalıyor.
- Bu paketi **sahibinin makinesinde** çalıştırmak gerekir; sandbox sonucu bunun yerine geçmez.

## Bilinen kalan noktalar (testlerle kapsanmayan veya bilerek kabul edilenler)

1. **Gerçek iç hatalar 422 görünebilir.** Pipeline içinde `TypeError`, `KeyError`, `AttributeError`, `IndexError`, `RecursionError`
   çıkarsa yanıt `422 invalid_input_shape` olur (girdi mi kod hatası mı ayrımı yapılmaz). Bu hata türlerinin log sayısı izlenmeli.
2. **Sahiplik (BOLA) kontrolü sezgiseldir.** Yanıtta `user_id`, `userId`, `owner_id` veya parametre/state anahtarı adında alan varsa kontrol edilir.
   Sahiplik başka bir alan adıyla taşınıyorsa yakalanmaz.
3. **Üretici adım düşünce bağımlı kontroller `skip` olur.** Bu bilinçli bir tasarımdır; CI'da `skipped > 0` durumunu başarısız saymak önerilir,
   çünkü gizlenen bir açık olabilir.
4. **Kart filtresi ~%2 meşru isteği reddedebilir.** IIN öneki ve Luhn'u birlikte geçen 16 haneli kimlikler (ör. `4...` ile başlayanlar) kart sayılır.
5. **SSRF kontrolü `base_url` için yalnızca metadata ve özel ağları engeller.** `100.64.0.0/10` gibi aralıklar geçer; URL hiç çağrılmadığı için etkisi yoktur.
6. **LLM zaman aşımı varsayılanı 12 sn**, `LLM_TIMEOUT_S` ile 0.1 ile 45 sn arasında ayarlanır. Gerçek model gecikmesi bu paketle ölçülmez.
7. Projenin kendi `tests/` ve `evals/run_evals.py` sonuçları bu pakete dahil değildir; `EVAL_REPORT.md` ile ayrıca karşılaştırılmalıdır.
