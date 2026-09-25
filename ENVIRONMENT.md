# إعداد المتغيرات السرية في Render

لا تضع أي قيمة حقيقية في GitHub أو في ملف ZIP. في Render افتح:

**الخدمة → Environment → Add Environment Variable**

## المتغيرات

| المتغير | المطلوب؟ | القيمة |
|---|---:|---|
| `APP_HOST` | لا | `0.0.0.0`، موجود في `render.yaml` |
| `META_GRAPH_VERSION` | لا | الإصدار الذي تدعمه Meta في حسابك، والافتراضي `v23.0` |
| `META_VERIFY_TOKEN` | عند تفعيل WhatsApp | قيمة طويلة عشوائية تختارها أنت؛ يجب أن تكون مطابقة في Meta Webhooks وفي Evoflow |
| `META_APP_ID` | فقط لـ Embedded Signup | App ID من Meta |
| `META_APP_SECRET` | فقط لـ Embedded Signup | App Secret من Meta، كقيمة Secret في Render |
| `META_CONFIG_ID` | فقط لـ Embedded Signup | Configuration ID من Facebook Login for Business |

`PORT` لا تضفه يدويًا؛ Render يحقنه تلقائيًا، والتطبيق يستمع على `0.0.0.0`.

## قواعد الأمان

- لا ترسل Gemini API Key أو Telegram Bot Token أو Meta Access Token داخل المحادثة.
- لا تحفظ الأسرار في `app.py` أو `index.html` أو GitHub.
- لا تطبع الأسرار في logs.
- إذا ظهر سر في Git، ألغِه من مزوده وأنشئ سرًا جديدًا؛ حذف الملف وحده لا يكفي من تاريخ Git.
- `META_APP_SECRET` لا يصل إلى المتصفح. الخادم فقط يستخدمه في مسار Embedded Signup.

## ملاحظة عن النسخة الحالية

النسخة المرفقة تحفظ إعدادات التجربة والأسرار في ذاكرة العملية فقط. لذلك قد تحتاج إلى إعادة ربط القنوات بعد restart أو redeploy. لا تستخدمها كـ SaaS متعدد العملاء قبل إضافة مصادقة وعزل العملاء وقاعدة Postgres وتخزين أسرار مشفر.
