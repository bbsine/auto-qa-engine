# Course Submission & Marketplace Release Notes

This document preserves the course rubric mapping, manual release checklist, and contributor declaration originally stored in `README.md` (written in Turkish for the midterm submission on branch `v-vize`). For developer onboarding, architecture, API reference, and running instructions, see [README.md](../README.md) ([Türkçe](../README.tr.md)).

---

## 1. Midterm Evaluation Rubric Mapping (100 Points / Vize Değerlendirme Rubriği)

| Ölçüt | Puan | Projedeki Karşılığı ve Kanıt Dosyaları |
| :--- | :---: | :--- |
| **Çalışırlık ve Sözleşme Uyumu** | 20 | [`Dockerfile`](../Dockerfile) (`USER 1000`), [`src/main.py`](../src/main.py) (`/health` < 2s, `/v1/invoke`), [`tests/test_contract.py`](../tests/test_contract.py) |
| **Fonksiyonel Değer** | 20 | [`src/parser.py`](../src/parser.py) (OpenAPI 3.x & Swagger 2.0), [`src/planner.py`](../src/planner.py) (ODG), [`src/generator.py`](../src/generator.py), [`src/validator.py`](../src/validator.py) (AST) |
| **Eval Seti** | 15 | [`evals/cases.yaml`](../evals/cases.yaml) (38 senaryo: 10 normal, 12 edge, 4 out_of_scope, 12 attack) ve [`EVAL_REPORT.md`](../EVAL_REPORT.md) |
| **Güvenlik** | 15 | [`src/security.py`](../src/security.py) (Constant-time HMAC, 300s replay, SSRF engelleme, Bölüm 4.3 kart/CVV/TCKN filtresi, AST RCE engelleme) |
| **Entegrasyon Talimatları** | 15 | [`integrations/common.md`](../integrations/common.md), [`integrations/claude-code.md`](../integrations/claude-code.md), [`integrations/codex.md`](../integrations/codex.md), [`integrations/cursor.mdc`](../integrations/cursor.mdc) |
| **Dokümantasyon ve Vitrin** | 10 | [`README.md`](../README.md), [`docs/DESCRIPTION.md`](DESCRIPTION.md), [`agent.yaml`](../agent.yaml), [`assets/`](../assets/icon.png) |
| **Sunum ve Demo** | 5 | `agent.yaml` içindeki `demo.samples` ile canlı ODG + Pytest üretim demosu |
| **TOPLAM** | **100** | **Hedef Sürüm Etiketi:** `v-vize` |

---

## 2. Release Checklist (Manual Steps for the Owner)

Teslimden önce proje sahibi tarafından bizzat çalıştırılması gereken adımlar:

1. [`agent.yaml`](../agent.yaml) içindeki `author.github` alanını gerçek GitHub kullanıcı adınızla doğrulayın.
2. [`integrations/common.md`](../integrations/common.md) Bölüm 4'teki doğrulama tablosunu, entegrasyon talimatlarını iki farklı gerçek projede denedikten sonra doldurun.
3. Docker imajını yerel makinenizde derleyip `/health` ve örnek `/v1/invoke` çağrısını doğrulayın:
   ```bash
   docker build -t auto-qa-engine:v-vize .
   ```
4. Değişiklikleri doğrulayıp `v-vize` etiketini (tag) oluşturun ve GitHub'a gönderin:
   ```bash
   git add .
   git status   # .env veya kişisel anahtar olmadığından emin olun
   git commit -m "feat: release auto-qa-engine (spec_version 1.1) for v-vize"
   git branch -M main
   git push -u origin main
   git tag v-vize
   git push origin v-vize
   ```

---

## 3. Developer & Contribution Declaration (Geliştirici ve Katkı Beyanı)

- **Geliştirici:** Berkay (`bbsine` — Bireysel Proje: mimari tasarım, deterministik ODG motoru, güvenlik katmanı, AST doğrulayıcı, 38 senaryoluk eval seti ve entegrasyon dokümantasyonu.)
