# ECG Recorder (Android)

Aplikacija za duge snimke jednokanalnog ECG-a sa USB (CH340) uređaja preko OTG kabla.
Snima u foreground servisu (radi i sa ugašenim ekranom), piše CSV koji ECG servis
(`example.com/ecg`) direktno analizira, i sama šalje snimak na server.

## Zašto postoji

- **Web Serial u browseru** radi samo dok je strana otvorena i ekran uključen; za
  10–30 min u ležećem položaju to puca (strana ode u pozadinu → veza se prekine).
- **WebView (in-app browser)** ne podržava ni Web Serial ni WebUSB — nikad neće raditi.
- Aplikacija koristi **Android USB Host API** (biblioteka `usb-serial-for-android`),
  pa nema zavisnosti od browsera.

## Funkcije

- **Živi talasni oblik** (`LiveView`): poslednjih ~5 s signala sa automatskom skalom, pp amplituda i
  stanje kontakta (DOBAR / SLAB / OTPALA ELEKTRODA / SATURACIJA) — koristi se pre snimanja da se
  proveri da li su elektrode dobro postavljene.
- **Povezivanje i snimanje su odvojeni**: `CONNECT` otvara port i samo prikazuje talas; snimanje
  počinje na `REC_START`, a `REC_STOP` zatvara fajl (veza ostaje otvorena za sledeći snimak).
- **Automatska pauza kad elektroda otpadne**: ravan signal (< 20 ADC jedinica pp) ili saturacija
  (> 50 % uzoraka na granici) duže od 3 s → upis se zaustavlja i u CSV ide
  `# PAUSE t=MM:SS label=elektroda otpala (ravna linija)`; kad kontakt bude dobar 1 s →
  `# RESUME`. Ručna pauza je odvojeno dugme; auto-pauza se može isključiti.
- 9600 baud, 8N1 (nema parametara za izbor — naš modul je fiksno tu).
- Vremenski žig **po uzorku**, ravnomerno 1/192 s unutar pročitanog chunka; nominalna
  fs 192 Hz (9600 baud / 5 bajtova po liniji).
- **Markeri faza**: dugmad upisuju `# MARK t=MM:SS label=...` u CSV — servis ih ignoriše
  (comment `#`), a `ecg_phases.py` ih čita i pripisuje puls uz svaki marker.
- Auto-reconnect do 20×2 s ako se CH340 resetuje (USB re-enumeracija).
- Živi prikaz: vreme, broj uzoraka, izmerena fs, procena pulsa.
- **Pošalji na servis** → multipart POST na `api.php?action=submit` (`data_file`, `fs=192`,
  `source=android-app`), pa ispis `?action=status`.
- Fajl ostaje lokalno: `Android/data/org.ecgrecorder/files/ecg/ECG_YYYYmmdd_HHMMSS.csv`

## Format snimka

```
Timestamp,S1
# MARK t=00:00.0 label=mirovanje
2026-10-16T11:00:00.000+02:00,505
2026-10-16T11:00:00.005+02:00,507
...
# STOP t=06:00 samples=69120 measured_fs=192.00 bad_lines=0
```

## Build (bez Android Studio)

```bash
apt-get install -y unzip openjdk-21-jdk
# Android SDK (cmdline-tools) u /opt/android-sdk; platform 34 + build-tools 34.0.0
gradle -p . assembleDebug   # from the android/ directory
# APK: app/build/outputs/apk/debug/app-debug.apk
```

`local.properties` sadrži `sdk.dir=/opt/android-sdk`.

## Verifikacija (bez telefona)

1. `gradle assembleDebug` prolazi → APK postoji.
2. `aapt2 dump badging app-debug.apk` → paket `org.ecgrecorder`, minSdk 24, targetSdk 34,
   dozvole (INTERNET, FOREGROUND_SERVICE, FOREGROUND_SERVICE_CONNECTED_DEVICE, WAKE_LOCK).
3. **Format snimka** je proveren protiv servisa: generisan je CSV tačno u formatu aplikacije
   (ISO+ms sa offsetom, `# MARK`, `# STOP`) i propušten kroz `ecg_analyze.py`
   (`--outdir /tmp/...`, ne dira `data/`) → 69.120 uzoraka, fs izmerena 192,0 Hz,
   „ISO timestamp kolona (69120 različitih vremena)"; markeri ne smetaju.
4. Isti snimak kroz `ecg_phases.py` → faze 60/100/75 bpm prepoznate tačno, a uz svaki
   marker ispisan puls u tom trenutku (npr. `2:00.0 posle fizičke aktivnosti → 100.2 bpm`).
5. **Test auto-pauze**: generisan je snimak u formatu aplikacije sa 30 s pauze (nema uzoraka između
   150. i 180. s, uz `# PAUSE ... label=elektroda otpala (ravna linija)` i `# RESUME`) →
   `ecg_phases.py` je prijavio fazu 60 bpm, zatim
   `PAUZE: 2:30.0-3:00.0 (30 s) elektroda otpala (ravna linija)`, pa fazu 90 bpm; segmenti koji
   padaju u pauzu se ne tumače (izuzeti iz faza).

⚠️ **Ceo snimak sa promenom stanja ne sme da se tumači iz glavnog izveštaja servisa**:
autokorelacija i provera periodičnosti padaju (u testu: „brojanje 75 vs autokorelacija 100,
razlika 25% → HR nepouzdan"), pa izveštaj (ispravno) odbija tumačenje. Za takve snimke je
merodavan `ecg_phases.py` (metrike po segmentima/fazama).

## Ograničenja

- Jedan kanal, AD8232, bez kalibracije u mV → morfologija i ST se ne tumače klinički.
- Android 14+ zahteva `foregroundServiceType="connectedDevice"` (deklarisano).
- USB dozvola se traži pri svakom novom priključivanju (osim ako je čekirana).
- Aplikacija nije na Play Store-u — instalira se kao APK (debug potpis).
