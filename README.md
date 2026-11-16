# FlashLab

Termux Android Flashing, Payload Dumper & Critical Partition Backup Suite.

## Fitur Utama
- Termux-USB OTG Auto-Wrapper (Non-Root): Menjalankan fastboot & adb lewat kabel OTG di Termux tanpa akses root.
- Native payload.bin Extractor: Ekstrak partisi (boot, init_boot, vbmeta, dll.) langsung dari payload.bin atau OTA .zip dengan verifikasi SHA-256.
- Auto-Parser flash_all.sh + Anti-Rollback Guard: Menjalankan script bawaan Fastboot ROM Xiaomi & Google Pixel dengan perlindungan anti-rollback.
- Critical Partition Backup Engine: Backup partisi persist, efs, modemst1/2, dan nvram via ADB/Recovery sebelum flashing.

## Instalasi Cepat (Satu Baris di Termux)

    curl -sSL https://raw.githubusercontent.com/xxpxx1/FlashLab/main/install.sh | bash

Setelah terpasang, cukup ketik flashlab di Termux untuk membukanya kapan saja.
