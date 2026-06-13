# NVR Monitor — мобильное приложение (Capacitor)

Нативная оболочка для Android/iOS, которая открывает веб-панель NVR Monitor,
работающую на сервере **внутри локальной сети предприятия**, и достукивается до
неё **из любого места** через приватную сеть [Tailscale](https://tailscale.com).

Сам интерфейс не переписан заново: приложение — это нативный установочный пакет
(`.apk` / `.ipa`) с экраном подключения, который после проверки связи загружает
уже существующую панель в системный webview. Весь функционал (дашборд, автобусы,
архив, алерты, PWA-вкладки) едет внутрь без изменений.

```
телефон (где угодно)            сервер в LAN предприятия
┌───────────────────┐  Tailscale  ┌──────────────────────────┐
│ NVR Monitor (apk) │◀──────────▶│ FastAPI :8000 ── NVR/камеры│
│  webview → панель │  (WireGuard)│  (локальные IP регистр.)  │
└───────────────────┘             └──────────────────────────┘
```

## Почему Tailscale

Сервер мониторинга должен видеть регистраторы по локальным IP, поэтому он живёт
внутри сети предприятия. Tailscale создаёт зашифрованную mesh-сеть (WireGuard)
между сервером и телефоном: **ничего не торчит в интернет**, не нужен белый IP и
проброс портов, доступ только у устройств в вашей сети (tailnet).

### 1. Сервер (внутри LAN)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# узнать адрес сервера в tailnet:
tailscale ip -4            # напр. 100.101.102.103
tailscale status           # MagicDNS-имя, напр. nvrmon.<tailnet>.ts.net
```

Сервер NVR Monitor должен слушать на всех интерфейсах (так и есть в Docker
Compose / `uvicorn --host 0.0.0.0`), порт по умолчанию `8000`.

> Совет: включите [MagicDNS](https://tailscale.com/kb/1081/magicdns) в админке
> Tailscale — тогда адрес сервера будет стабильным именем
> `http://<hostname>.<tailnet>.ts.net:8000`, а не «голым» IP.

### 2. Телефон

Поставьте официальное приложение **Tailscale** (Google Play / App Store),
войдите в тот же аккаунт/tailnet и включите VPN. После этого телефон видит сервер
по его tailnet-адресу из любой точки мира.

## Сборка приложения

Нужны Node.js 18+, а также Android Studio (для Android) и/или Xcode + CocoaPods
(для iOS). Этот каталог — корень Capacitor-проекта.

```bash
cd mobile
npm install

# (опционально) сгенерировать иконки/сплэш из resources/icon.png:
npx capacitor-assets generate --iconBackgroundColor '#0f1419' --iconBackgroundColorDark '#0f1419'

# Android
npx cap add android
npx cap sync android
npx cap open android        # дальше Build → APK/AAB в Android Studio
# или сразу debug-apk из консоли:
cd android && ./gradlew assembleDebug
# готовый файл: android/app/build/outputs/apk/debug/app-debug.apk

# iOS (только на macOS)
npx cap add ios
npx cap sync ios
npx cap open ios            # подпись и сборка в Xcode
```

Каталоги `android/` и `ios/` не хранятся в git (см. `.gitignore`) — они
воссоздаются командой `cap add`. Версионируются только исходники оболочки в
`www/`, конфиг и ресурсы.

## Подписанный release-APK

`assembleDebug` даёт debug-сборку (подписана временным ключом). Для нормальной
раздачи на телефоны сотрудников нужен **release**, подписанный своим ключом.
Один раз создайте keystore (храните его и пароли — без них нельзя выпускать
обновления):

```bash
keytool -genkeypair -v \
  -keystore ~/newapp/mobile/android/app/nvrmon-release.keystore \
  -alias nvrmon -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass 'ВАШ_ПАРОЛЬ' -keypass 'ВАШ_ПАРОЛЬ' \
  -dname "CN=NVR Monitor, O=MCC, C=RU"
```

Параметры подписи кладутся в `android/app/keystore.properties` (этот файл и
keystore **не** коммитятся), а `android/app/build.gradle` ссылается на них в
`signingConfigs.release`. Затем:

```bash
cd ~/newapp/mobile/android
./gradlew assembleRelease
# готовый файл: android/app/build/outputs/apk/release/app-release.apk
```

> Эти правки (`keystore.properties`, блок `signingConfigs` в `build.gradle`)
> живут в каталоге `android/`, которого нет в git, — настраиваются один раз на
> сборочной машине.

## HTTP-сервер без TLS (cleartext)

Если сервер отдаёт по `http://` (без TLS), Android по умолчанию блокирует такой
трафик из приложения. Один раз на сборочной машине разрешите cleartext:

```bash
# 1. сетевая политика
mkdir -p android/app/src/main/res/xml
cat > android/app/src/main/res/xml/network_security_config.xml <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors><certificates src="system" /></trust-anchors>
    </base-config>
</network-security-config>
EOF
```

И добавьте в тег `<application>` в `android/app/src/main/AndroidManifest.xml`
атрибуты `android:usesCleartextTraffic="true"` и
`android:networkSecurityConfig="@xml/network_security_config"`. Эти файлы лежат в
`android/` (нет в git), так что правки делаются один раз на сборочной машине.

## Как пользоваться

Адрес сервера по умолчанию **зашит** (`www/connect.js` → `DEFAULT_SERVER`),
поэтому приложение работает как обычное нативное: запустил — сразу открылась
панель, без ввода адресов «как в браузере».

1. Включите VPN (WireGuard/Tailscale) на телефоне, если вы вне сети предприятия.
2. Откройте NVR Monitor — загрузится панель, спросит логин/пароль
   (`ADMIN_PASSWORD`).
3. Если сервер недоступен, вместо панели покажется экран с пояснением и полем
   для другого адреса.

### Сменить адрес сервера прямо в приложении

Адрес можно поменять **без пересборки**:

1. В шапке панели нажмите **«⚙»** (видна только в приложении) — или системную
   кнопку **«Назад»**. Откроется экран «Настройки подключения» с текущим адресом.
2. Впишите новый адрес (напр. `https://nvr.example.com` или
   `http://nvrmon.<tailnet>.ts.net:8000`) → «Подключиться». Адрес сохранится и
   будет использоваться дальше.
3. Кнопка **«Сбросить на адрес по умолчанию»** возвращает зашитый адрес.

> Кнопка «⚙» появляется, потому что оболочка помечает себя в User-Agent
> (`appendUserAgent: "NVRMonApp"`), а шаблон панели показывает её только при
> наличии этой метки. На обычном вебе/в браузере кнопки нет.

Чтобы поменять сам адрес по умолчанию (для новых установок), правьте
`DEFAULT_SERVER` в `www/connect.js` и пересоберите.

`allowNavigation` ограничивает webview адресами локальных сетей и VPN —
приложение не уйдёт на сторонние сайты. Сервер по умолчанию отдаёт http без TLS
(трафик шифрует VPN), поэтому в приложении разрешён cleartext-трафик
(`network_security_config.xml`, см. ниже).

## Что внутри

| Файл | Назначение |
|---|---|
| `capacitor.config.json` | id приложения, тема, `allowNavigation` (tailnet + LAN), splash/статус-бар |
| `www/index.html` · `connect.css` · `connect.js` | экран подключения и автологин по сохранённому адресу |
| `resources/icon.png` | исходник для генерации иконок/сплэша |

`allowNavigation` ограничивает webview адресами Tailscale (`*.ts.net`, `100.*`)
и приватных сетей — приложение не уйдёт на сторонние сайты. `allowMixedContent`
включён, потому что Tailscale отдаёт http без TLS (трафик и так шифрует WireGuard).

## Идеи для следующих шагов

- Нативные пуш-уведомления (Capacitor Push + FCM) вместо/вместе с Telegram —
  потребует регистрации токена устройства на сервере.
- Биометрическая блокировка входа в приложение (`capacitor-native-biometric`).
- Удержание экрана во включённом состоянии в TV-режиме (`@capacitor/keep-awake`).
