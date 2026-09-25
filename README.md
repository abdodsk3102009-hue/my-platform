# Evoflow — حزمة Render عربية RTL

هذه حزمة MVP قابلة للرفع إلى GitHub ثم الربط مع **Render Web Service**. التطبيق لا يحتاج npm ولا Docker ولا مكتبات Python خارجية في هذه النسخة؛ يستخدم Python Standard Library فقط.

> **مهم:** هذه نسخة تشغيل وتجربة، وليست SaaS إنتاجيًا متعدد العملاء. الأسرار وإعدادات القنوات تبقى في RAM، لذلك تضيع عند إعادة تشغيل الخدمة أو إعادة نشرها. Render Free نفسه يستخدم نظام ملفات مؤقتًا ولا يصلح لحفظ بيانات العملاء.

## ما الذي يعمل فعليًا؟

- لوحة عربية RTL وWidget قابل للتضمين في موقع عام.
- `GET /health` لفحص صحة الخدمة.
- استخدام `PORT` الذي يحقنه Render والاستماع على `0.0.0.0`.
- Gemini وأي API متوافق مع OpenAI عبر BYOK.
- System Prompt حقيقي يمر إلى المزود قبل رسالة المستخدم.
- fallback بين المزودات عند الحصة أو الرفض أو timeout أو أخطاء الخادم.
- Telegram: فحص Bot Token ومحاولة تسجيل Webhook تلقائيًا عندما يكون الرابط العام HTTPS.
- Website crawler محدود: حتى 25 صفحة عامة من نفس النطاق، مع منع localhost والشبكات الداخلية، واستخدام الصفحات المرتبطة بالسؤال كسياق.
- WhatsApp Cloud API الرسمي:
  - مسار تجربة Meta Test Number.
  - ربط يدوي مؤقت بواسطة Phone Number ID وWABA ID وAccess Token وVerify Token.
  - بعد التحقق يحاول الخادم تفعيل اشتراك التطبيق في Webhooks على WABA عبر `/{WABA_ID}/subscribed_apps`.
  - Webhook للتحقق واستقبال الرسائل وتمريرها إلى System Prompt ثم إرسال الرد عبر Meta؛ يجب أيضًا إعداد Callback URL وحقل `messages` من لوحة Meta.
  - هيكل Embedded Signup الرسمي، بما في ذلك تبادل الكود على الخادم، عندما تجهز Meta App والصلاحيات.
- لا يوجد OAuth ملتف، ولا Session Tokens، ولا تدوير حسابات لتجاوز حصص Google.

## ١. النشر على Render من GitHub

### أ. رفع المشروع

ضع **محتويات هذا المجلد** في جذر مستودع GitHub، بحيث يكون `app.py` و`render.yaml` في المستوى الأول:

```bash
git init
git add .
git commit -m "Prepare Evoflow for Render"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

قبل `git add` تأكد أن المفاتيح ليست في أي ملف. ملف `.gitignore` مرفق لذلك، لكن راجعه يدويًا.

### ب. إنشاء الخدمة

لديك مساران صحيحان:

**المسار الموصى به مع `render.yaml`:**

1. افتح Render وأنشئ **New → Blueprint**.
2. اربط GitHub واختر المستودع.
3. سيقرأ Render `render.yaml` ويقترح Web Service باسم `evoflow`.
4. أدخل قيم المتغيرات التي عليها `sync: false` عند الطلب، خصوصًا `META_VERIFY_TOKEN` إذا ستستخدم WhatsApp.
5. وافق على إنشاء الخدمة وانتظر Build ثم Deploy.

**المسار اليدوي:**

1. افتح Render وأنشئ **New → Web Service**.
2. اربط GitHub واختر المستودع.
3. أدخل:
   - Runtime: `Python`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python app.py`
   - Health Check Path: `/health`
   - Plan: Free للتجربة فقط.
4. اترك `PORT` بدون قيمة؛ Render يحقنه تلقائيًا.
5. أضف المتغيرات من [ENVIRONMENT.md](ENVIRONMENT.md). لا تضع القيم الحقيقية في GitHub.
6. اضغط **Create Web Service** وانتظر Build ثم Deploy.

بعد النشر اختبر:

```text
https://YOUR-SERVICE.onrender.com/health
```

يجب أن يعيد JSON فيه `"ok": true`. افتح بعدها رابط الخدمة نفسه، وليس رابط معاينة محليًا أو محميًا.

## ٢. كيف أعدّل الموقع بعد أن يعمل؟

المسار الصحيح:

1. عدّل `app.py` أو `index.html` محليًا.
2. اختبر:
   ```bash
   python3 -m py_compile app.py
   PORT=8090 python3 app.py
   ```
3. افتح `http://127.0.0.1:8090` واختبر `/health` وWidget.
4. نفّذ:
   ```bash
   git add app.py index.html
   git commit -m "Describe the change"
   git push
   ```
5. إذا كان Auto Deploy فعالًا، يبدأ Render Build وDeploy تلقائيًا من آخر commit.
6. راقب **Events** و**Logs** في Render. بعد النجاح اختبر رابط `.onrender.com`.

تظل النسخة الحالية متاحة حتى يكتمل النشر الجديد غالبًا، لكن لا تعتمد على ذلك في تغييرات قاعدة بيانات. كل restart/redeploy قد يمسح البيانات الموجودة في RAM.

يمكن تعديل Environment Variables من:

**Render → الخدمة → Environment**

ثم احفظ وأعد النشر عند طلب Render. لا تعدّل الأسرار داخل Git. إذا احتجت تغيير شكل اللوحة، غيّر `index.html`; وإذا احتجت منطق الخادم، غيّر `app.py` ثم `git push`.

## ٣. حدود Render Free

- الخدمة قد تدخل في Spin Down بعد 15 دقيقة بلا طلبات، وقد يتأخر أول طلب حتى تستيقظ.
- نظام الملفات مؤقت؛ لا تعتمد على SQLite أو JSON أو الملفات المحلية.
- قد تفقد العملية ذاكرتها عند restart/redeploy.
- الخطة مناسبة للعرض التجريبي وMVP، وليست تخزينًا دائمًا لإعدادات العملاء.
- قبل إطلاق SaaS حقيقي أضف Postgres لتخزين المواقع والإعدادات، مصادقة وعزل tenants، تشفير الأسرار أو Secret Manager، rate limiting، queue، ونسخًا احتياطية. لا تعرض هذه الحزمة على أنها تخزين دائم.

## ٤. WhatsApp — المسار الرسمي الأقل تعقيدًا

### المرحلة الأولى: اختبار مجاني بحساب Meta Test Number

هذه أسرع بداية ولا تحتاج رقم Business حقيقي:

1. افتح [Meta for Developers](https://developers.facebook.com/) وأنشئ/افتح App.
2. أضف منتج WhatsApp.
3. من **WhatsApp → API Setup** استخدم Test Number.
4. أضف رقمك في خانة المستلم؛ تتيح Meta أرقام اختبار محدودة للتطوير.
5. أنشئ Access Token مؤقتًا للاختبار أو Token مناسبًا حسب إعداد Meta.
6. في Evoflow استخدم **WhatsApp → تجربة Meta المجانية** للتعليمات، ثم **ربط يدوي مؤقت** لإدخال:
   - `Phone Number ID`
   - `WABA ID` أو Messaging Account ID؛ مطلوب لتفعيل اشتراك Webhook تلقائيًا
   - `Access Token`؛ يمكن استخدام Token الاختبار المؤقت في البداية
   - `Verify Token` تختاره أنت
7. اضغط **تحقق واربط**. الخادم يستدعي Graph API بدل قبول قيم غير مفحوصة.
8. في Meta Webhooks استخدم:
   - Callback URL: `https://YOUR-SERVICE.onrender.com/api/meta/webhook`
   - Verify Token: نفس `META_VERIFY_TOKEN` أو نفس القيمة التي أدخلتها في النموذج.
9. اشترك في حقل الرسائل `messages`، ثم أرسل رسالة من رقم الاختبار إلى رقم Meta.

رسائل الاختبار أثناء التطوير قد تكون مجانية، لكن WhatsApp Cloud API ليست مجانية بلا حدود. الرسائل الحقيقية والقوالب والسوق ونوع المحادثة ونافذة خدمة العميل تخضع لتسعير وشروط Meta، والفوترة تكون على إعداد حساب Meta المعني.

### المرحلة الثانية: ربط العميل دون نسخ Token

المسار الرسمي الأفضل للمنتج هو **Embedded Signup**:

1. تنشئ Meta App لـ Evoflow.
2. تفعّل Facebook Login for Business وWhatsApp Cloud API.
3. تنشئ Configuration وتضع Domain Allowlist وRedirect URI على رابط HTTPS العام.
4. تطلب الصلاحيات والمراجعة وAdvanced Access المطلوبين.
5. تضيف في Render:
   - `META_APP_ID`
   - `META_APP_SECRET`
   - `META_CONFIG_ID`
6. زر Embedded Signup في الواجهة يفتح نافذة Meta الرسمية، ويستقبل code، ثم يرسله إلى `/api/meta/embedded/exchange`.
7. الخادم يستبدل الكود عبر Meta دون كشف App Secret للمتصفح، ثم يتحقق من Phone Number ID ويربط Webhook.

لا يجوز عرض هذا المسار للعملاء على أنه يعمل فورًا بضغطة واحدة قبل إنجاز متطلبات Meta، وخاصة وضع Tech Provider/المسار التجاري المناسب، App Review وAdvanced Access وHTTPS. الحزمة تحتوي الهيكل البرمجي، لكن موافقة Meta وحسابها ليستا شيئًا يمكن وضعه داخل ZIP.

## ٥. الملفات المهمة

- `app.py`: الخادم والـ API وWidget وcrawler وTelegram وWhatsApp.
- `index.html`: لوحة التحكم العربية.
- `evoflow-logo.png`: الشعار.
- `render.yaml`: إعداد Render المقترح.
- `requirements.txt`: ملف build؛ لا توجد حزم خارجية حاليًا.
- `.gitignore`: منع الأسرار والملفات المحلية.
- `.env.example`: أسماء المتغيرات فقط بلا أسرار.
- `ENVIRONMENT.md`: تعليمات المتغيرات السرية.

## ٦. اختبار محلي سريع

```bash
cd product
python3 -m py_compile app.py
PORT=8090 python3 app.py
```

في طرفية ثانية:

```bash
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:8090/api/config | python3 -m json.tool
curl -fsS -X POST http://127.0.0.1:8090/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"كم السعر؟"}]}'
```

إذا لم تضف مفتاحًا، يستخدم الطلب العرض المحلي Demo. هذا لا يثبت أن Gemini أو WhatsApp يعملان؛ لاختبارهما أدخل الأسرار من الواجهة أو Environment الخاصة بالخدمة، ولا ترسلها في المحادثة.
