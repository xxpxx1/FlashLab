import os
import re
import sys
import json
import shlex
import shutil
import struct
import bz2
import lzma
import logging
import time
import hashlib
import subprocess
import random
import zipfile
import tarfile
from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn
)

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

console = Console()

CRITICAL_PARTITIONS = {
    "persist", "persistbak",
    "efs", "sec_efs", "efs_backup",
    "modemst1", "modemst2", "fsg", "fsc",
    "nvram", "nvdata", "nvcfg", "protect1", "protect2", "proinfo",
    "frp", "misc", "param", "cdt", "ddb"
}

# --- Utilitas Umum & Logger ---
def setup_logger():
    os.makedirs('logs', exist_ok=True)
    log_filename = datetime.now().strftime("logs/flashlab_%Y%m%d_%H%M%S.log")
    logging.basicConfig(
        filename=log_filename,
        level=logging.DEBUG,
        format="%(asctime)s - [%(levelname)s] - %(message)s"
    )
    usb_fd = os.environ.get("TERMUX_USB_FD", "NONE")
    logging.info(f"FlashLab v2.4 dimulai | TERMUX_USB_FD={usb_fd}")
    return log_filename

def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"

def run_cmd(cmd, timeout=None):
    """Wrapper eksekusi command dengan logging otomatis"""
    logging.info(f"EXEC: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.stdout.strip():
            logging.debug(f"STDOUT: {res.stdout.strip()}")
        if res.stderr.strip():
            logging.debug(f"STDERR: {res.stderr.strip()}")
        return res
    except subprocess.TimeoutExpired:
        logging.error(f"TIMEOUT: {' '.join(cmd)}")
        return None
    except Exception as e:
        logging.error(f"EXCEPTION ({' '.join(cmd)}): {e}")
        return None

def has_existing_backups():
    """Mengecek apakah sudah ada folder hasil backup di backups/"""
    if not os.path.exists("backups"):
        return False
    for entry in os.listdir("backups"):
        sub = os.path.join("backups", entry)
        if os.path.isdir(sub) and any(f.endswith(".img") for f in os.listdir(sub)):
            return True
    return False

# --- Modul OTG: Deteksi & Wrapper Otomatis termux-usb ---
def is_termux_env():
    return "com.termux" in os.environ.get("PREFIX", "") or os.path.exists("/data/data/com.termux")

def get_active_usb_fd():
    return os.environ.get("TERMUX_USB_FD")

def detect_otg_devices():
    """Mendeteksi perangkat USB OTG menggunakan termux-usb -l"""
    if shutil.which("termux-usb") is None:
        return "NO_TERMUX_USB", []

    try:
        res = subprocess.run(
            ["termux-usb", "-l"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if res.returncode != 0:
            logging.error(f"termux-usb -l error: {res.stderr}")
            return "ERROR", []

        raw_out = res.stdout.strip()
        if not raw_out:
            return "OK", []

        devices = json.loads(raw_out)
        return "OK", devices if isinstance(devices, list) else []
    except subprocess.TimeoutExpired:
        logging.error("termux-usb -l timeout (APK Termux:API kemungkinan belum terpasang)")
        return "API_TIMEOUT", []
    except json.JSONDecodeError:
        logging.error("Gagal mem-parsing output JSON dari termux-usb -l")
        return "ERROR", []

def bind_termux_usb_session(usb_device_path):
    """Membungkus ulang proses flashlab.py ke dalam termux-usb untuk mewariskan TERMUX_USB_FD"""
    script_path = os.path.abspath(sys.argv[0])
    python_bin = sys.executable
    inner_cmd = f"{shlex.quote(python_bin)} {shlex.quote(script_path)}"

    console.print(f"[bold cyan]⚡ Mengikat akses USB OTG: {usb_device_path}...[/bold cyan]")
    console.print("[dim]Jika muncul pop-up izin USB di layar Android, tekan OK / Izinkan.[/dim]")
    logging.info(f"Re-exec via termux-usb pada {usb_device_path}")
    time.sleep(0.8)

    try:
        os.execvp("termux-usb", ["termux-usb", "-r", "-E", "-e", inner_cmd, usb_device_path])
    except Exception as e:
        logging.error(f"Gagal execvp termux-usb: {e}")
        console.print(f"[bold red]✖ Gagal menjalankan wrapper termux-usb: {e}[/bold red]")

def otg_usb_manager():
    """Menu interaktif untuk deteksi & binding perangkat OTG non-root"""
    console.print("\n[bold cyan]=== TERMUX-USB OTG MANAGER (NON-ROOT) ===[/bold cyan]")
    current_fd = get_active_usb_fd()
    if current_fd:
        console.print(f"[bold green]✔ Sesi saat ini sudah terikat ke USB File Descriptor: FD={current_fd}[/bold green]")
        console.print("[dim]Catatan: Jika HP baru saja reboot (misal masuk FastbootD), lakukan Re-Bind.[/dim]\n")

    status, usb_list = detect_otg_devices()

    if status == "NO_TERMUX_USB":
        console.print("[bold red]✖ Paket 'termux-api' belum terpasang di Termux.[/bold red]")
        console.print("Jalankan perintah berikut di Termux:\n[yellow]pkg update && pkg install termux-api libusb android-tools[/yellow]")
        return
    elif status == "API_TIMEOUT":
        console.print("[bold red]✖ Koneksi ke Termux:API timeout![/bold red]")
        console.print("Pastikan aplikasi [yellow]Termux:API (APK)[/yellow] sudah terpasang di Android dan tidak dibatasi baterai.")
        return
    elif status == "ERROR":
        console.print("[bold red]✖ Gagal membaca daftar perangkat dari termux-usb.[/bold red]")
        return

    if not usb_list:
        console.print("[bold yellow]⚠ Tidak ada perangkat USB OTG yang terdeteksi.[/bold yellow]")
        console.print("Pastikan kabel OTG terpasang kencang dan fitur OTG di pengaturan HP host aktif.")
        return

    table = Table(title="Perangkat USB OTG Terdeteksi", style="cyan")
    table.add_column("No", style="dim", width=4)
    table.add_column("Device Bus Path", style="green")
    for idx, dev in enumerate(usb_list, 1):
        table.add_row(str(idx), dev)
    console.print(table)

    pilihan = IntPrompt.ask(
        "[bold green]Pilih nomor perangkat OTG untuk di-bind (0 untuk batal)[/bold green]",
        default=1 if len(usb_list) == 1 else 0
    )
    if pilihan <= 0 or pilihan > len(usb_list):
        return

    bind_termux_usb_session(usb_list[pilihan - 1])

# --- Modul 1: Device Manager & Inspector ---
def scan_devices(auto_prompt_otg=True):
    """Mendeteksi perangkat Fastboot & ADB, serta otomatis menawarkan OTG wrapper jika diperlukan"""
    has_fb = shutil.which('fastboot') is not None
    has_adb = shutil.which('adb') is not None

    if not has_fb and not has_adb:
        return "NOT_INSTALLED", None

    connected = []
    active_mode = None

    if has_fb:
        res_fb = run_cmd(['fastboot', 'devices'], timeout=5)
        if res_fb and res_fb.stdout.strip():
            for line in res_fb.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and 'fastboot' in parts[1]:
                    connected.append(f"{parts[0]} [bold yellow](Fastboot Mode)[/bold yellow]")
                    active_mode = 'fastboot'

    if has_adb:
        res_adb = run_cmd(['adb', 'devices'], timeout=5)
        if res_adb and res_adb.stdout.strip():
            lines = res_adb.stdout.strip().splitlines()[1:]
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    serial, state = parts[0], parts[1]
                    if state == 'unauthorized':
                        connected.append(f"{serial} [bold red](ADB Unauthorized - Cek layar HP target!)[/bold red]")
                    elif state == 'offline':
                        connected.append(f"{serial} [dim red](ADB Offline)[/dim red]")
                    else:
                        connected.append(f"{serial} [bold cyan](ADB / {state.capitalize()})[/bold cyan]")
                        if not active_mode:
                            active_mode = 'adb'

    if not connected and auto_prompt_otg and is_termux_env() and not get_active_usb_fd():
        status, usb_list = detect_otg_devices()
        if status == "OK" and usb_list:
            console.print(f"\n[bold yellow]🔌 Terdeteksi {len(usb_list)} perangkat USB OTG ({usb_list[0]}) yang belum di-bind ke Termux![/bold yellow]")
            if Confirm.ask("[bold cyan]Aktifkan wrapper termux-usb sekarang agar fastboot/adb bisa mengakses OTG?[/bold cyan]", default=True):
                if len(usb_list) == 1:
                    bind_termux_usb_session(usb_list[0])
                else:
                    otg_usb_manager()

    return connected, active_mode

def get_fastboot_var(var_name):
    """Mengambil nilai variabel fastboot getvar dengan bersih"""
    res = run_cmd(['fastboot', 'getvar', var_name], timeout=5)
    if res and res.stderr:
        for line in res.stderr.splitlines():
            if line.lower().startswith(f"{var_name.lower()}:"):
                return line.split(":", 1)[1].strip()
    return None

def inspect_device(simulation_mode, device_mode):
    """Mengecek informasi krusial perangkat (Unlock status, Codename, Slot, ARB)"""
    table = Table(title="Informasi Perangkat Target", style="cyan")
    table.add_column("Parameter", style="bold yellow")
    table.add_column("Nilai / Status", style="green")

    if simulation_mode:
        table.add_row("Mode Koneksi", "SIMULASI (Virtual Device)")
        table.add_row("Codename / Product", "sweet (Simulasi)")
        table.add_row("Bootloader Unlocked", "[bold green]yes[/bold green]")
        table.add_row("Anti-Rollback (anti)", "1 (Simulasi)")
        table.add_row("Active Slot", "a")
        console.print(table)
        return

    if device_mode == 'fastboot':
        vars_to_check = ['product', 'unlocked', 'current-slot', 'battery-voltage', 'anti']
        for var in vars_to_check:
            val = get_fastboot_var(var) or "N/A"
            if var == 'unlocked':
                val = f"[bold green]{val}[/bold green]" if val.lower() == 'yes' else f"[bold red]{val} (BAHAYA FLASHING!)[/bold red]"
            table.add_row(var.upper(), val)
    elif device_mode == 'adb':
        props = {
            "Codename": "ro.product.device",
            "Model": "ro.product.model",
            "Android Version": "ro.build.version.release",
            "Bootloader State": "ro.boot.verifiedbootstate",
            "Slot Suffix": "ro.boot.slot_suffix"
        }
        for label, prop in props.items():
            res = run_cmd(['adb', 'shell', 'getprop', prop], timeout=5)
            val = res.stdout.strip() if res and res.stdout.strip() else "N/A"
            table.add_row(label, val)

    console.print(table)

def wireless_adb_menu():
    """Pairing & Connect ADB via Wi-Fi langsung dari Termux"""
    console.print("\n[bold cyan]=== WIRELESS ADB (ANDROID 11+) ===[/bold cyan]")
    console.print("1. Pair Perangkat Baru (Butuh IP:Port Pairing & Kode 6 Digit)")
    console.print("2. Connect ke Perangkat (Butuh IP:Port Utama)")
    console.print("3. Disconnect Semua Wireless ADB")
    console.print("0. Kembali")

    choice = IntPrompt.ask("[bold green]Pilih aksi[/bold green]", choices=["0", "1", "2", "3"])
    if choice == 0:
        return
    elif choice == 1:
        ip_port = Prompt.ask("Masukkan IP:Port Pairing (contoh 192.168.1.5:37123)")
        code = Prompt.ask("Masukkan 6 digit kode pairing")
        res = run_cmd(['adb', 'pair', ip_port, code], timeout=15)
        if res and res.returncode == 0:
            console.print(f"[bold green]✔ {res.stdout.strip()}[/bold green]")
        else:
            console.print(f"[bold red]✖ Gagal pairing:[/bold red] {res.stderr if res else 'Timeout'}")
    elif choice == 2:
        ip_port = Prompt.ask("Masukkan IP:Port Koneksi (contoh 192.168.1.5:41235)")
        res = run_cmd(['adb', 'connect', ip_port], timeout=10)
        if res and "connected" in res.stdout.lower():
            console.print(f"[bold green]✔ {res.stdout.strip()}[/bold green]")
        else:
            console.print(f"[bold red]✖ Gagal connect:[/bold red] {res.stdout if res else 'Timeout'}")
    elif choice == 3:
        run_cmd(['adb', 'disconnect'], timeout=5)
        console.print("[yellow]Semua koneksi Wireless ADB diputus.[/yellow]")

# --- Modul 1B: Critical Partition Backup Engine (ADB / Recovery) ---
def check_adb_root_capability():
    """Mengecek apakah adbd berjalan sebagai root (Custom Recovery) atau via su"""
    res_id = run_cmd(["adb", "shell", "id"], timeout=5)
    if res_id and "uid=0" in res_id.stdout:
        return "direct_root"

    res_su = run_cmd(["adb", "shell", "su", "-c", "id"], timeout=6)
    if res_su and "uid=0" in res_su.stdout:
        return "su_root"

    return None

def discover_critical_blocks(root_method):
    """Memindai partisi krusial (persist, efs, modemst1/2, nvram, dll.) pada perangkat target"""
    probe_script = (
        'target_dir=""; '
        'for d in /dev/block/by-name /dev/block/bootdevice/by-name; do '
        '  if [ -d "$d" ]; then target_dir="$d"; break; fi; '
        'done; '
        'if [ -z "$target_dir" ]; then '
        '  target_dir=$(find /dev/block/platform -name "by-name" -type d 2>/dev/null | head -n 1); '
        'fi; '
        'if [ -n "$target_dir" ]; then '
        '  for f in "$target_dir"/*; do '
        '    [ -e "$f" ] || continue; '
        '    name=$(basename "$f"); '
        '    real=$(readlink -f "$f" 2>/dev/null || echo "$f"); '
        '    sz=$(blockdev --getsize64 "$real" 2>/dev/null || echo 0); '
        '    echo "$name|$real|$sz"; '
        '  done; '
        'fi'
    )

    if root_method == "direct_root":
        cmd = ["adb", "shell", probe_script]
    else:
        cmd = ["adb", "shell", "su", "-c", probe_script]

    res = run_cmd(cmd, timeout=15)
    if not res or not res.stdout.strip():
        return []

    found = []
    seen_names = set()
    for line in res.stdout.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name, real_dev, raw_sz = parts[0].strip(), parts[1].strip(), parts[2].strip()
        base_name = re.sub(r'_[ab]$', '', name.lower())
        if base_name in CRITICAL_PARTITIONS and name not in seen_names:
            seen_names.add(name)
            size_bytes = int(raw_sz) if raw_sz.isdigit() else 0
            found.append({
                "name": name,
                "block_path": real_dev,
                "size_bytes": size_bytes,
                "size_str": format_size(size_bytes) if size_bytes > 0 else "Unknown"
            })

    return sorted(found, key=lambda x: x["name"])

def stream_dump_partition(part_info, out_path, root_method, progress_cb=None):
    """Melakukan streaming raw partition via adb exec-out + menghitung SHA-256"""
    real_dev = part_info["block_path"]
    expected_size = part_info["size_bytes"]
    dd_inner = f"dd if={shlex.quote(real_dev)} bs=4096 2>/dev/null"

    if root_method == "direct_root":
        cmd = ["adb", "exec-out", dd_inner]
    else:
        cmd = ["adb", "exec-out", f"su -c {shlex.quote(dd_inner)}"]

    logging.info(f"STREAM DUMP: {' '.join(cmd)} -> {out_path}")
    sha256 = hashlib.sha256()
    written = 0

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with open(out_path, "wb") as out_f:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                out_f.write(chunk)
                sha256.update(chunk)
                written += len(chunk)
                if progress_cb and expected_size > 0:
                    pct = min((written / expected_size) * 100.0, 99.9)
                    progress_cb(pct)
        stderr_out = proc.stderr.read().decode("utf-8", errors="ignore").strip()
        ret = proc.wait(timeout=15)
    except Exception as e:
        proc.kill()
        raise RuntimeError(f"Error saat streaming blok: {e}")

    if ret != 0 or written == 0:
        if os.path.exists(out_path) and written == 0:
            os.remove(out_path)
        raise RuntimeError(f"Dump gagal (bytes={written}, rc={ret}): {stderr_out}")

    return written, sha256.hexdigest()

def adb_critical_backup_menu(simulation_mode, device_mode):
    """Menu interaktif untuk backup partisi krusial (persist, efs, modemst1/2, nvram)"""
    console.print("\n[bold cyan]=== CRITICAL PARTITION BACKUP (PERSIST / EFS / MODEMST) ===[/bold cyan]")
    console.print("[dim]Mencadangkan identitas IMEI, kalibrasi sensor, & baseband sebelum flashing.[/dim]\n")

    if simulation_mode:
        sim_parts = [
            {"name": "persist", "block_path": "/dev/block/sda18", "size_bytes": 33554432, "size_str": "32.00 MB"},
            {"name": "efs", "block_path": "/dev/block/sda12", "size_bytes": 20971520, "size_str": "20.00 MB"},
            {"name": "modemst1", "block_path": "/dev/block/sdf1", "size_bytes": 3145728, "size_str": "3.00 MB"},
            {"name": "modemst2", "block_path": "/dev/block/sdf2", "size_bytes": 3145728, "size_str": "3.00 MB"},
            {"name": "fsg", "block_path": "/dev/block/sdf3", "size_bytes": 3145728, "size_str": "3.00 MB"},
            {"name": "fsc", "block_path": "/dev/block/sdf4", "size_bytes": 131072, "size_str": "128.00 KB"}
        ]
        table = Table(title="Partisi Krusial Terdeteksi (Mode Simulasi)", style="cyan")
        table.add_column("No", style="dim", width=4)
        table.add_column("Partisi", style="bold green")
        table.add_column("Block Node", style="yellow")
        table.add_column("Ukuran", justify="right", style="cyan")
        for idx, p in enumerate(sim_parts, 1):
            table.add_row(str(idx), p["name"], p["block_path"], p["size_str"])
        console.print(table)

        if Confirm.ask("[bold green]Jalankan simulasi backup seluruh partisi di atas?[/bold green]", default=True):
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=35, style="magenta", complete_style="green"),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console
            ) as progress:
                for p in sim_parts:
                    tid = progress.add_task(f"[cyan]Simulasi dump {p['name']}.img...", total=100)
                    for _ in range(5):
                        time.sleep(0.06)
                        progress.update(tid, advance=20)
                    progress.update(tid, completed=100, description=f"[bold green]✔ {p['name']}.img ({p['size_str']})[/bold green]")
            console.print(Panel.fit("[bold green]✔ Simulasi backup partisi krusial selesai![/bold green]", border_style="green"))
        return

    if device_mode != "adb":
        console.print("[bold red]✖ Modul backup membutuhkan koneksi ADB / Custom Recovery (TWRP/OrangeFox)![/bold red]")
        console.print("Saat ini perangkat berada di mode [yellow]Fastboot[/yellow]. Reboot ke Recovery terlebih dahulu via Menu 9.")
        return

    with console.status("[bold magenta]Memeriksa hak akses root ADB / Custom Recovery...[/bold magenta]", spinner="dots"):
        root_method = check_adb_root_capability()

    if not root_method:
        console.print(Panel.fit(
            "[bold red]✖ Akses Root ADB tidak tersedia![/bold red]\n"
            "Untuk membaca blok partisi mentah (/dev/block), salah satu kondisi berikut wajib terpenuhi:\n"
            "1. Boot perangkat ke [bold cyan]Custom Recovery (TWRP / OrangeFox)[/bold cyan], atau\n"
            "2. Aktifkan izin [bold cyan]Root Shell (Magisk / KernelSU)[/bold cyan] untuk ADB.",
            border_style="red"
        ))
        return

    mode_label = "Custom Recovery / ADB Root" if root_method == "direct_root" else "Rooted OS (su)"
    console.print(f"[bold green]✔ Akses blok terverifikasi:[/bold green] {mode_label}")

    with console.status("[bold cyan]Memindai tabel partisi krusial pada perangkat...[/bold cyan]", spinner="bouncingBar"):
        partitions = discover_critical_blocks(root_method)

    if not partitions:
        console.print("[bold red]✖ Tidak ditemukan partisi krusial di /dev/block/by-name.[/bold red]")
        return

    table = Table(title=f"Ditemukan {len(partitions)} Partisi Krusial", style="cyan")
    table.add_column("No", style="dim", width=4)
    table.add_column("Nama Partisi", style="bold green")
    table.add_column("Block Device", style="yellow")
    table.add_column("Ukuran", justify="right", style="cyan")
    for idx, p in enumerate(partitions, 1):
        table.add_row(str(idx), p["name"], p["block_path"], p["size_str"])
    console.print(table)

    console.print("[dim]Masukkan nomor partisi (contoh: 1,2,3) atau ketik 'all' untuk mem-backup semuanya.[/dim]")
    raw_sel = Prompt.ask("[bold green]Pilih partisi yang akan di-backup[/bold green]", default="all").strip().lower()

    if raw_sel == "all":
        selected = partitions
    else:
        try:
            indices = [int(x.strip()) - 1 for x in raw_sel.split(",") if x.strip().isdigit()]
            selected = [partitions[i] for i in indices if 0 <= i < len(partitions)]
        except Exception:
            selected = []

    if not selected:
        console.print("[bold red]✖ Pilihan partisi tidak valid.[/bold red]")
        return

    # Ambil codename perangkat untuk penamaan folder backup
    res_codename = run_cmd(["adb", "shell", "getprop", "ro.product.device"], timeout=5)
    codename = re.sub(r'[^a-zA-Z0-9_-]', '', res_codename.stdout.strip()) if (res_codename and res_codename.stdout.strip()) else "android_device"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join("backups", f"{codename}_{timestamp}")
    os.makedirs(backup_dir, exist_ok=True)

    manifest = {
        "device_codename": codename,
        "timestamp": timestamp,
        "root_method": root_method,
        "partitions": []
    }

    success_count = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=35, style="magenta", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        for part in selected:
            tid = progress.add_task(f"[cyan]Dumping {part['name']}.img...", total=100)
            out_file = os.path.join(backup_dir, f"{part['name']}.img")
            try:
                written_bytes, sha_hex = stream_dump_partition(
                    part,
                    out_file,
                    root_method,
                    progress_cb=lambda pct, t=tid: progress.update(t, completed=pct)
                )
                manifest["partitions"].append({
                    "name": part["name"],
                    "file": f"{part['name']}.img",
                    "block_path": part["block_path"],
                    "size_bytes": written_bytes,
                    "sha256": sha_hex
                })
                success_count += 1
                progress.update(
                    tid,
                    completed=100,
                    description=f"[bold green]✔ {part['name']}.img ({format_size(written_bytes)} | {sha_hex[:10]}...)[/bold green]"
                )
            except Exception as e:
                logging.error(f"Backup gagal pada {part['name']}: {e}")
                progress.update(tid, description=f"[bold red]✖ {part['name']}.img GAGAL: {e}[/bold red]")

    manifest_path = os.path.join(backup_dir, "backup_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)

    console.print(Panel.fit(
        f"[bold green]✔ Backup selesai: {success_count}/{len(selected)} partisi tersimpan![/bold green]\n"
        f"Lokasi Backup : [cyan]{backup_dir}/[/cyan]\n"
        f"Manifest Hash : [yellow]{manifest_path}[/yellow]\n"
        "[dim]Simpan folder backups/ ini ke tempat aman (cloud/PC) sebelum memulai flashing.[/dim]",
        border_style="green"
    ))

# --- Modul 2: Auto Extractor (.zip / .tgz) ---
def extract_firmware():
    archive_dir = 'archives'
    os.makedirs(archive_dir, exist_ok=True)
    archives = [f for f in os.listdir(archive_dir) if f.endswith(('.zip', '.tgz', '.tar.gz', '.tar'))]

    if not archives:
        console.print(f"[bold red]✖ Tidak ada file arsip di folder {archive_dir}/.[/bold red]")
        return

    table = Table(title="File Arsip Tersedia", style="cyan")
    table.add_column("No", style="dim", width=4)
    table.add_column("Nama File Arsip", style="green")
    table.add_column("Ukuran", justify="right", style="yellow")

    for idx, arch in enumerate(archives):
        size = format_size(os.path.getsize(os.path.join(archive_dir, arch)))
        table.add_row(str(idx + 1), arch, size)
    console.print(table)

    pilihan = IntPrompt.ask("[bold green]Pilih nomor arsip untuk diekstrak (Ketik 0 batal)[/bold green]")
    if pilihan <= 0 or pilihan > len(archives):
        return

    selected_archive = archives[pilihan - 1]
    archive_path = os.path.join(archive_dir, selected_archive)
    logging.info(f"Mengekstrak arsip: {archive_path}")

    try:
        os.makedirs('firmware', exist_ok=True)
        if selected_archive.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                with console.status("[bold magenta]Mengekstrak ZIP...[/bold magenta]", spinner="bouncingBar"):
                    zip_ref.extractall('firmware')
        elif selected_archive.endswith(('.tgz', '.tar.gz', '.tar')):
            with tarfile.open(archive_path, 'r:*') as tar_ref:
                with console.status("[bold magenta]Mengekstrak TAR/TGZ...[/bold magenta]", spinner="bouncingBar"):
                    tar_ref.extractall('firmware')
        console.print("[bold green]✔ Ekstraksi berhasil masuk ke folder firmware/.[/bold green]")
    except Exception as e:
        logging.error(f"Gagal ekstrak {selected_archive}: {e}")
        console.print(f"[bold red]✖ Gagal mengekstrak: {e}[/bold red]")

# --- Modul 2B: Pure-Python CrAU Payload.bin Dumper ---
def _pb_read_varint(buf, pos):
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Varint protobuf terpotong")

def _pb_skip_field(buf, pos, wire_type):
    if wire_type == 0:
        _, pos = _pb_read_varint(buf, pos)
    elif wire_type == 1:
        pos += 8
    elif wire_type == 2:
        length, pos = _pb_read_varint(buf, pos)
        pos += length
    elif wire_type == 5:
        pos += 4
    else:
        raise ValueError(f"Wire type protobuf tidak didukung: {wire_type}")
    return pos

def _pb_parse_extent(buf):
    pos = 0
    start_block = 0
    num_blocks = 0
    while pos < len(buf):
        tag, pos = _pb_read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if field_num == 1 and wire_type == 0:
            start_block, pos = _pb_read_varint(buf, pos)
        elif field_num == 2 and wire_type == 0:
            num_blocks, pos = _pb_read_varint(buf, pos)
        else:
            pos = _pb_skip_field(buf, pos, wire_type)
    return {"start_block": start_block, "num_blocks": num_blocks}

def _pb_parse_operation(buf):
    pos = 0
    op_type = 0
    data_offset = 0
    data_length = 0
    dst_extents = []
    while pos < len(buf):
        tag, pos = _pb_read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if field_num == 1 and wire_type == 0:
            op_type, pos = _pb_read_varint(buf, pos)
        elif field_num == 2 and wire_type == 0:
            data_offset, pos = _pb_read_varint(buf, pos)
        elif field_num == 3 and wire_type == 0:
            data_length, pos = _pb_read_varint(buf, pos)
        elif field_num == 6 and wire_type == 2:
            length, pos = _pb_read_varint(buf, pos)
            dst_extents.append(_pb_parse_extent(buf[pos:pos + length]))
            pos += length
        else:
            pos = _pb_skip_field(buf, pos, wire_type)
    return {
        "type": op_type,
        "data_offset": data_offset,
        "data_length": data_length,
        "dst_extents": dst_extents
    }

def _pb_parse_partition_info(buf):
    pos = 0
    size = 0
    part_hash = b""
    while pos < len(buf):
        tag, pos = _pb_read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if field_num == 1 and wire_type == 0:
            size, pos = _pb_read_varint(buf, pos)
        elif field_num == 2 and wire_type == 2:
            length, pos = _pb_read_varint(buf, pos)
            part_hash = buf[pos:pos + length]
            pos += length
        else:
            pos = _pb_skip_field(buf, pos, wire_type)
    return {"size": size, "hash": part_hash}

def _pb_parse_partition_update(buf):
    pos = 0
    name = "unknown"
    new_info = {"size": 0, "hash": b""}
    operations = []
    while pos < len(buf):
        tag, pos = _pb_read_varint(buf, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if field_num == 1 and wire_type == 2:
            length, pos = _pb_read_varint(buf, pos)
            name = buf[pos:pos + length].decode("utf-8", errors="replace")
            pos += length
        elif field_num == 7 and wire_type == 2:
            length, pos = _pb_read_varint(buf, pos)
            new_info = _pb_parse_partition_info(buf[pos:pos + length])
            pos += length
        elif field_num == 8 and wire_type == 2:
            length, pos = _pb_read_varint(buf, pos)
            operations.append(_pb_parse_operation(buf[pos:pos + length]))
            pos += length
        else:
            pos = _pb_skip_field(buf, pos, wire_type)

    incremental_ops = {4, 5, 9, 10}
    is_incremental = any(op["type"] in incremental_ops for op in operations)
    uses_zstd = any(op["type"] == 11 for op in operations)

    return {
        "name": name,
        "size": new_info["size"],
        "hash": new_info["hash"],
        "operations": operations,
        "is_incremental": is_incremental,
        "uses_zstd": uses_zstd
    }

def parse_crau_payload(payload_path):
    """Membaca header & manifest CrAU dari payload.bin"""
    with open(payload_path, "rb") as f:
        magic = f.read(4)
        if magic != b"CrAU":
            raise ValueError("File bukan format CrAU payload.bin yang valid!")
        version = struct.unpack(">Q", f.read(8))[0]
        manifest_size = struct.unpack(">Q", f.read(8))[0]
        metadata_sig_size = 0
        if version >= 2:
            metadata_sig_size = struct.unpack(">I", f.read(4))[0]
        manifest_bytes = f.read(manifest_size)
        f.seek(metadata_sig_size, os.SEEK_CUR)
        data_offset_base = f.tell()

    pos = 0
    block_size = 4096
    partitions = []
    while pos < len(manifest_bytes):
        tag, pos = _pb_read_varint(manifest_bytes, pos)
        field_num, wire_type = tag >> 3, tag & 0x07
        if field_num == 3 and wire_type == 0:
            block_size, pos = _pb_read_varint(manifest_bytes, pos)
        elif field_num == 13 and wire_type == 2:
            length, pos = _pb_read_varint(manifest_bytes, pos)
            partitions.append(_pb_parse_partition_update(manifest_bytes[pos:pos + length]))
            pos += length
        else:
            pos = _pb_skip_field(manifest_bytes, pos, wire_type)

    return {
        "version": version,
        "block_size": block_size,
        "data_offset_base": data_offset_base,
        "partitions": partitions
    }

def extract_single_partition(payload_file_obj, part_info, block_size, data_offset_base, out_dir, progress_cb=None):
    """Mengekstrak satu partisi dari payload.bin secara streaming hemat RAM"""
    out_path = os.path.join(out_dir, f"{part_info['name']}.img")
    ops = part_info["operations"]
    total_ops = max(len(ops), 1)
    zstd_dctx = zstd.ZstdDecompressor() if (part_info["uses_zstd"] and HAS_ZSTD) else None

    with open(out_path, "wb") as out_f:
        for idx, op in enumerate(ops):
            op_type = op["type"]
            if op_type in (4, 5, 9, 10):
                raise RuntimeError(f"Partisi '{part_info['name']}' menggunakan Incremental OTA (butuh base ROM).")

            raw_chunk = b""
            if op["data_length"] > 0:
                payload_file_obj.seek(data_offset_base + op["data_offset"])
                raw_chunk = payload_file_obj.read(op["data_length"])

            if op_type == 0:
                decompressed = raw_chunk
            elif op_type == 1:
                decompressed = bz2.decompress(raw_chunk)
            elif op_type == 8:
                decompressed = lzma.decompress(raw_chunk)
            elif op_type == 11:
                if not zstd_dctx:
                    raise RuntimeError("Payload memakai kompresi ZSTD. Jalankan di Termux: pip install zstandard")
                decompressed = zstd_dctx.decompress(raw_chunk)
            elif op_type in (6, 7):
                decompressed = None
            else:
                raise RuntimeError(f"Tipe operasi CrAU tidak dikenal: {op_type}")

            buf_offset = 0
            for ext in op["dst_extents"]:
                target_byte_pos = ext["start_block"] * block_size
                target_byte_len = ext["num_blocks"] * block_size
                out_f.seek(target_byte_pos)
                if decompressed is None:
                    out_f.write(b"\x00" * target_byte_len)
                else:
                    out_f.write(decompressed[buf_offset:buf_offset + target_byte_len])
                    buf_offset += target_byte_len

            if progress_cb and (idx % 5 == 0 or idx == total_ops - 1):
                progress_cb(((idx + 1) / total_ops) * 100)

        if part_info["size"] > 0:
            out_f.truncate(part_info["size"])

    verified = None
    if part_info["hash"]:
        actual_hash = hashlib.sha256()
        with open(out_path, "rb") as vf:
            for block in iter(lambda: vf.read(65536), b""):
                actual_hash.update(block)
        verified = (actual_hash.digest() == part_info["hash"])

    return out_path, verified

def payload_bin_extractor_menu():
    """Menu interaktif ekstraktor payload.bin (Standalone maupun dari OTA .zip)"""
    console.print("\n[bold cyan]=== PAYLOAD.BIN EXTRACTOR (TERMUX NATIVE ENGINE) ===[/bold cyan]")
    os.makedirs("archives", exist_ok=True)
    os.makedirs("firmware", exist_ok=True)

    candidates = []
    for folder in ["archives", "firmware"]:
        for root, _, files in os.walk(folder):
            for fname in files:
                fpath = os.path.join(root, fname)
                if fname == "payload.bin":
                    candidates.append({"type": "BIN", "label": fpath, "path": fpath})
                elif fname.endswith(".zip") and folder == "archives":
                    try:
                        with zipfile.ZipFile(fpath, "r") as zf:
                            if "payload.bin" in zf.namelist():
                                candidates.append({"type": "ZIP", "label": f"{fpath} (berisi payload.bin)", "path": fpath})
                    except Exception:
                        pass

    if not candidates:
        console.print("[bold red]✖ Tidak ditemukan file 'payload.bin' atau OTA '.zip' yang berisi payload.bin di folder archives/ maupun firmware/.[/bold red]")
        return

    table = Table(title="Sumber Payload.bin Tersedia", style="cyan")
    table.add_column("No", style="dim", width=4)
    table.add_column("Tipe", style="yellow", width=6)
    table.add_column("Lokasi File", style="green")
    for idx, item in enumerate(candidates, 1):
        table.add_row(str(idx), item["type"], item["label"])
    console.print(table)

    pilihan = IntPrompt.ask("[bold green]Pilih sumber payload (0 untuk batal)[/bold green]", default=1)
    if pilihan <= 0 or pilihan > len(candidates):
        return

    selected = candidates[pilihan - 1]
    payload_path = selected["path"]

    if selected["type"] == "ZIP":
        extracted_payload = os.path.join("archives", "payload.bin")
        console.print(f"[bold yellow]⚡ Mengekstrak payload.bin dari {payload_path}...[/bold yellow]")
        try:
            with zipfile.ZipFile(payload_path, "r") as zf:
                zinfo = zf.getinfo("payload.bin")
                total_bytes = zinfo.file_size
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[cyan]Mengeluarkan payload.bin dari ZIP..."),
                    BarColumn(bar_width=35),
                    TaskProgressColumn(),
                    TimeElapsedColumn(),
                    console=console
                ) as prog:
                    task = prog.add_task("extract", total=total_bytes)
                    with zf.open("payload.bin") as src_f, open(extracted_payload, "wb") as dst_f:
                        while True:
                            chunk = src_f.read(1024 * 1024)
                            if not chunk:
                                break
                            dst_f.write(chunk)
                            prog.update(task, advance=len(chunk))
            payload_path = extracted_payload
            console.print(f"[bold green]✔ payload.bin siap di {payload_path}[/bold green]")
        except Exception as e:
            logging.error(f"Gagal mengeluarkan payload.bin dari zip: {e}")
            console.print(f"[bold red]✖ Gagal mengekstrak payload.bin dari ZIP: {e}[/bold red]")
            return

    try:
        with console.status("[bold magenta]Membaca manifest CrAU payload.bin...[/bold magenta]", spinner="dots"):
            manifest = parse_crau_payload(payload_path)
    except Exception as e:
        logging.error(f"Gagal parse payload.bin: {e}")
        console.print(f"[bold red]✖ Gagal membaca payload.bin: {e}[/bold red]")
        return

    partitions = manifest["partitions"]
    if not partitions:
        console.print("[bold red]✖ Tidak ada partisi ditemukan di dalam manifest payload.bin.[/bold red]")
        return

    if any(p["uses_zstd"] for p in partitions) and not HAS_ZSTD:
        console.print("[bold yellow]⚠ Payload ini menggunakan kompresi ZSTD (Android 15/16+). Pasang dulu: pip install zstandard[/bold yellow]")

    ptable = Table(title=f"Daftar Partisi di Payload.bin (v{manifest['version']} | Block {manifest['block_size']}B)", style="magenta")
    ptable.add_column("No", style="dim", width=4)
    ptable.add_column("Nama Partisi", style="bold cyan")
    ptable.add_column("Ukuran Output", justify="right", style="green")
    ptable.add_column("Tipe OTA", style="yellow")

    for idx, p in enumerate(partitions, 1):
        ota_type = "[red]Incremental (Butuh Base)[/red]" if p["is_incremental"] else "[green]Full OTA[/green]"
        ptable.add_row(str(idx), p["name"], format_size(p["size"]), ota_type)
    console.print(ptable)

    console.print("[dim]Tips: Masukkan nomor (misal: 1,4), nama partisi (misal: boot,init_boot,vbmeta), atau 'all'.[/dim]")
    raw_sel = Prompt.ask("[bold green]Pilih partisi yang ingin diekstrak[/bold green]", default="boot,init_boot,vendor_boot,vbmeta").strip().lower()

    selected_parts = []
    if raw_sel == "all":
        selected_parts = partitions
    else:
        tokens = [t.strip() for t in raw_sel.split(",") if t.strip()]
        for tok in tokens:
            if tok.isdigit():
                idx = int(tok) - 1
                if 0 <= idx < len(partitions) and partitions[idx] not in selected_parts:
                    selected_parts.append(partitions[idx])
            else:
                for p in partitions:
                    if p["name"].lower() == tok and p not in selected_parts:
                        selected_parts.append(p)

    if not selected_parts:
        console.print("[bold red]✖ Tidak ada partisi yang cocok dengan pilihanmu.[/bold red]")
        return

    out_dir = os.path.join("firmware", "payload_extracted")
    os.makedirs(out_dir, exist_ok=True)
    logging.info(f"Mengekstrak {len(selected_parts)} partisi dari {payload_path} ke {out_dir}")

    with open(payload_path, "rb") as pf, Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=35, style="magenta", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        for part in selected_parts:
            tid = progress.add_task(f"[cyan]Mengekstrak {part['name']}.img...", total=100)
            try:
                _, verified = extract_single_partition(
                    pf,
                    part,
                    manifest["block_size"],
                    manifest["data_offset_base"],
                    out_dir,
                    progress_cb=lambda pct, t=tid: progress.update(t, completed=pct)
                )
                badge = "SHA256 OK" if verified else ("No Hash" if verified is None else "HASH MISMATCH!")
                style = "bold green" if verified is not False else "bold red"
                progress.update(tid, completed=100, description=f"[{style}]✔ {part['name']}.img ({badge})[/{style}]")
            except Exception as e:
                logging.error(f"Gagal ekstrak partisi {part['name']}: {e}")
                progress.update(tid, description=f"[bold red]✖ {part['name']}.img GAGAL: {e}[/bold red]")

    console.print(Panel.fit(
        f"[bold green]✔ Proses ekstraksi payload selesai![/bold green]\n"
        f"Hasil tersimpan di: [cyan]{out_dir}/[/cyan]\n"
        "Jalankan [bold yellow]Menu 5 (Load Firmware)[/bold yellow] untuk memuat file .img hasil ekstrak.",
        border_style="green"
    ))

# --- Modul 2C: Parser & Executor flash_all.sh (Xiaomi & Pixel) ---
def find_flash_scripts(base_dir="firmware"):
    """Mencari file flash_all*.sh atau flash-all.sh secara rekursif di folder firmware/"""
    scripts = []
    if not os.path.exists(base_dir):
        return scripts
    for root, _, files in os.walk(base_dir):
        for fname in sorted(files):
            if fname.endswith(".sh") and "flash" in fname.lower():
                scripts.append(os.path.join(root, fname))
    return scripts

def parse_flash_script(script_path):
    """Mem-parsing file flash_all.sh (Xiaomi MIUI/HyperOS) maupun flash-all.sh (Google Pixel)"""
    script_dir = os.path.dirname(os.path.abspath(script_path))
    with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
        raw_lines = f.readlines()

    expected_products = []
    required_anti_ver = None
    wipes_userdata = False
    relocks_bootloader = False
    operations = []
    missing_files = []

    for line_no, raw_line in enumerate(raw_lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        anti_match = re.search(r'CURRENT_ANTI_VER\s*=\s*(\d+)', line)
        if anti_match:
            required_anti_ver = int(anti_match.group(1))
            continue

        if "getvar" in line and "product" in line and "grep" in line:
            prod_match = re.search(r'product:\s*\*?\(?([a-zA-Z0-9_|.-]+)\)?', line)
            if prod_match:
                for p in prod_match.group(1).split("|"):
                    p_clean = p.strip()
                    if p_clean and p_clean not in expected_products:
                        expected_products.append(p_clean)
            continue

        if "fastboot" not in line or "getvar" in line:
            continue

        clean_line = re.sub(r'\$\(which\s+fastboot\)', 'fastboot', line)
        clean_line = re.sub(r'\|\|.*$', '', clean_line)
        clean_line = re.sub(r'&&.*$', '', clean_line)
        clean_line = re.sub(r'\d*>&\d+', '', clean_line)
        clean_line = clean_line.replace('$*', '').replace('"$@"', '').replace('$@', '').strip()

        try:
            tokens = shlex.split(clean_line)
        except ValueError:
            tokens = clean_line.split()

        if not tokens or not tokens[0].endswith("fastboot"):
            continue

        args = tokens[1:]
        if not args:
            continue

        has_wipe_flag = False
        filtered_args = []
        for a in args:
            if a == "-w":
                has_wipe_flag = True
                wipes_userdata = True
            else:
                filtered_args.append(a)

        if has_wipe_flag:
            operations.append({
                "line": line_no,
                "action": "wipe",
                "target": "userdata (-w)",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", "-w"]
            })

        if not filtered_args:
            continue

        subcmd = filtered_args[0]

        if subcmd == "flash":
            flags = []
            rest = []
            for a in filtered_args[1:]:
                if a.startswith("-"):
                    flags.append(a)
                else:
                    rest.append(a)
            if len(rest) >= 2:
                partition, rel_file = rest[0], rest[1]
                abs_file = os.path.normpath(os.path.join(script_dir, rel_file))
                if not os.path.exists(abs_file):
                    missing_files.append(rel_file)
                if partition.lower() == "userdata":
                    wipes_userdata = True
                operations.append({
                    "line": line_no,
                    "action": "flash",
                    "target": partition,
                    "file_rel": rel_file,
                    "file_abs": abs_file,
                    "flags": flags,
                    "cmd": ["fastboot", "flash"] + flags + [partition, abs_file]
                })

        elif subcmd == "erase" and len(filtered_args) >= 2:
            partition = filtered_args[1]
            if partition.lower() == "userdata":
                wipes_userdata = True
            operations.append({
                "line": line_no,
                "action": "erase",
                "target": partition,
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", "erase", partition]
            })

        elif subcmd == "update" and len(filtered_args) >= 2:
            rel_zip = filtered_args[1]
            abs_zip = os.path.normpath(os.path.join(script_dir, rel_zip))
            if not os.path.exists(abs_zip):
                missing_files.append(rel_zip)
            operations.append({
                "line": line_no,
                "action": "update",
                "target": "OTA/Image Package",
                "file_rel": rel_zip,
                "file_abs": abs_zip,
                "flags": [],
                "cmd": ["fastboot", "update", abs_zip]
            })

        elif subcmd == "set_active" and len(filtered_args) >= 2:
            slot = filtered_args[1]
            operations.append({
                "line": line_no,
                "action": "set_active",
                "target": f"Slot {slot}",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", "set_active", slot]
            })

        elif subcmd.startswith("--set-active="):
            slot = subcmd.split("=", 1)[1]
            operations.append({
                "line": line_no,
                "action": "set_active",
                "target": f"Slot {slot}",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", subcmd]
            })

        elif subcmd in ("oem", "flashing") and len(filtered_args) >= 2:
            sub_action = filtered_args[1]
            if sub_action == "lock":
                relocks_bootloader = True
            operations.append({
                "line": line_no,
                "action": f"{subcmd} {sub_action}",
                "target": "Bootloader Security",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", subcmd, sub_action]
            })

        elif subcmd == "reboot-bootloader" or (subcmd == "reboot" and len(filtered_args) >= 2 and filtered_args[1] == "bootloader"):
            operations.append({
                "line": line_no,
                "action": "reboot-bootloader",
                "target": "Bootloader Stage",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", "reboot-bootloader"]
            })

        elif subcmd == "reboot" and len(filtered_args) == 1:
            operations.append({
                "line": line_no,
                "action": "reboot",
                "target": "System",
                "file_rel": "-",
                "file_abs": None,
                "flags": [],
                "cmd": ["fastboot", "reboot"]
            })

    return {
        "script_path": script_path,
        "script_name": os.path.basename(script_path),
        "expected_products": expected_products,
        "required_anti_ver": required_anti_ver,
        "wipes_userdata": wipes_userdata,
        "relocks_bootloader": relocks_bootloader,
        "operations": operations,
        "missing_files": missing_files
    }

def flash_all_script_menu(simulation_mode, device_mode):
    """Menu interaktif untuk membaca, memvalidasi ARB, dan mengeksekusi flash_all.sh"""
    console.print("\n[bold cyan]=== AUTO-PARSER SCRIPT FLASH_ALL.SH (XIAOMI / PIXEL) ===[/bold cyan]")
    scripts = find_flash_scripts("firmware")

    if not scripts:
        console.print("[bold red]✖ Tidak ditemukan file flash_all*.sh atau flash-all.sh di folder firmware/.[/bold red]")
        console.print("[dim]Ekstrak terlebih dahulu arsip Fastboot ROM (.tgz / .zip) melalui Menu 4.[/dim]")
        return

    table = Table(title="Script Fastboot ROM Ditemukan", style="cyan")
    table.add_column("No", style="dim", width=4)
    table.add_column("Nama Script", style="bold green")
    table.add_column("Lokasi Folder", style="white")
    for idx, sp in enumerate(scripts, 1):
        table.add_row(str(idx), os.path.basename(sp), os.path.dirname(sp))
    console.print(table)

    pilihan = IntPrompt.ask("[bold green]Pilih script yang akan dianalisis (0 untuk batal)[/bold green]", default=1)
    if pilihan <= 0 or pilihan > len(scripts):
        return

    parsed = parse_flash_script(scripts[pilihan - 1])
    logging.info(f"Mem-parsing script: {parsed['script_path']} | Ops={len(parsed['operations'])} | ARB={parsed['required_anti_ver']}")

    meta_table = Table(title=f"Hasil Analisis: {parsed['script_name']}", style="magenta")
    meta_table.add_column("Parameter", style="bold yellow")
    meta_table.add_column("Detail", style="cyan")

    prod_str = ", ".join(parsed["expected_products"]) if parsed["expected_products"] else "Tidak didefinisikan di script"
    anti_str = str(parsed["required_anti_ver"]) if parsed["required_anti_ver"] is not None else "Tidak ada pengecekan ARB"
    wipe_str = "[bold red]YA (Data Internal Akan Terhapus)[/bold red]" if parsed["wipes_userdata"] else "[bold green]TIDAK (Data Aman / Keep Data)[/bold green]"
    lock_str = "[bold red]⚠ YA (MENGUNCI ULANG BOOTLOADER!)[/bold red]" if parsed["relocks_bootloader"] else "[green]Tidak (Tetap Unlocked)[/green]"

    meta_table.add_row("Target Codename", prod_str)
    meta_table.add_row("Min. Anti-Rollback (ARB)", anti_str)
    meta_table.add_row("Hapus Userdata", wipe_str)
    meta_table.add_row("Relock Bootloader", lock_str)
    meta_table.add_row("Total Urutan Perintah", f"{len(parsed['operations'])} langkah")
    console.print(meta_table)

    ops_table = Table(title="Urutan Eksekusi Partisi dari Script", style="blue")
    ops_table.add_column("Step", style="dim", width=5)
    ops_table.add_column("Aksi", style="bold yellow", width=18)
    ops_table.add_column("Target / Partisi", style="cyan")
    ops_table.add_column("File Image / Berkas", style="green")
    for idx, op in enumerate(parsed["operations"], 1):
        ops_table.add_row(str(idx), op["action"].upper(), op["target"], op["file_rel"])
    console.print(ops_table)

    if parsed["missing_files"]:
        console.print(Panel.fit(
            "[bold red]✖ EKSEKUSI DIBATALKAN: Ada file firmware yang hilang![/bold red]\n"
            + "\n".join(f" - {mf}" for mf in parsed["missing_files"][:10]),
            border_style="red"
        ))
        return

    if not parsed["operations"]:
        console.print("[bold red]✖ Tidak ada perintah fastboot valid yang berhasil diparsing dari script ini.[/bold red]")
        return

    if not simulation_mode and not has_existing_backups():
        console.print("[bold yellow]⚠ Belum ditemukan backup partisi krusial (persist/efs/modemst) di folder backups/.[/bold yellow]")
        if not Confirm.ask("[bold cyan]Lanjutkan tanpa backup partisi krusial? (Pilih 'n' untuk backup dulu via Menu 13)[/bold cyan]", default=True):
            return

    skip_relock = False
    if parsed["relocks_bootloader"]:
        console.print(Panel.fit(
            "[bold red]⚠ PERINGATAN KRITIS: Script ini mengandung perintah 'oem lock'![/bold red]\n"
            "Mengunci bootloader pada ROM wilayah berbeda (misal ROM Global di HP China) atau ROM modifikasi\n"
            "akan menyebabkan perangkat terkunci permanen (Hard-Brick).",
            border_style="red"
        ))
        if Confirm.ask("[bold yellow]Lewati (abaikan) perintah 'oem lock' agar bootloader tetap terbuka?[/bold yellow]", default=True):
            skip_relock = True

    if not simulation_mode:
        if device_mode != "fastboot":
            console.print("[bold red]✖ Perangkat tidak berada dalam mode Fastboot! Masuk ke Bootloader/Fastboot terlebih dahulu.[/bold red]")
            return

        unlocked_val = get_fastboot_var("unlocked")
        if unlocked_val and unlocked_val.lower() != "yes":
            console.print(f"[bold red]✖ Bootloader perangkat masih terkunci (unlocked: {unlocked_val}). Flashing dibatalkan.[/bold red]")
            return

        dev_product = get_fastboot_var("product")
        if dev_product and parsed["expected_products"]:
            if dev_product not in parsed["expected_products"]:
                console.print(f"[bold red]⚠ PERINGATAN CODENAME TIDAK COCOK![/bold red] Perangkat: [yellow]{dev_product}[/yellow] | Script ROM: [cyan]{', '.join(parsed['expected_products'])}[/cyan]")
                if not Confirm.ask("[bold red]Tetap paksa lanjutkan meskipun codename berbeda? (Berisiko Brick!)[/bold red]", default=False):
                    return
            else:
                console.print(f"[bold green]✔ Verifikasi Codename cocok:[/bold green] {dev_product}")

        if parsed["required_anti_ver"] is not None:
            raw_anti = get_fastboot_var("anti")
            dev_anti = int(raw_anti) if (raw_anti and raw_anti.isdigit()) else 0
            req_anti = parsed["required_anti_ver"]
            logging.info(f"ARB Check: Device={dev_anti}, Script={req_anti}")

            if dev_anti > req_anti:
                console.print(Panel.fit(
                    f"[bold red]✖ ANTI-ROLLBACK CHECK GAGAL (DIBLOKIR DEMI KEAMANAN)![/bold red]\n"
                    f"Versi Anti-Rollback HP Target : [bold yellow]{dev_anti}[/bold yellow]\n"
                    f"Versi Anti-Rollback ROM Script : [bold cyan]{req_anti}[/bold cyan]\n\n"
                    "Mem-flash ROM dengan versi ARB lebih rendah daripada perangkat akan memicu\n"
                    "kegagalan boot permanen (Anti-Rollback Brick). Eksekusi dihentikan.",
                    border_style="red"
                ))
                return
            else:
                console.print(f"[bold green]✔ Anti-Rollback aman:[/bold green] Device ({dev_anti}) / ROM ({req_anti})")

    if not Confirm.ask(f"[bold red]⚠ Jalankan seluruh {len(parsed['operations'])} langkah dari '{parsed['script_name']}' sekarang?[/bold red]"):
        return

    console.print(f"\n[bold yellow]⚡ Mengeksekusi Script '{parsed['script_name']}' ({'SIMULASI' if simulation_mode else 'REAL HARDWARE'})...[/bold yellow]")
    failed_steps = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=35, style="magenta", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        main_task = progress.add_task("[cyan]Menjalankan urutan script...", total=len(parsed["operations"]))

        for idx, op in enumerate(parsed["operations"], 1):
            action = op["action"]
            target = op["target"]

            if skip_relock and "lock" in action:
                progress.update(main_task, advance=1, description=f"[yellow]⏭ Melewati {action} (Sesuai opsi aman)[/yellow]")
                continue

            if action == "reboot":
                progress.update(main_task, advance=1, description="[green]✔ Menyiapkan final reboot...[/green]")
                continue

            progress.update(main_task, description=f"[cyan][{idx}/{len(parsed['operations'])}] {action.upper()} -> {target}...[/cyan]")

            if simulation_mode:
                time.sleep(0.15)
                progress.update(main_task, advance=1)
            else:
                res = run_cmd(op["cmd"])
                if res and res.returncode == 0:
                    progress.update(main_task, advance=1)
                    if action == "reboot-bootloader":
                        progress.stop()
                        console.print("[bold yellow]⏳ Perangkat sedang reboot ke Bootloader (jeda 5 detik)...[/bold yellow]")
                        time.sleep(5)
                        if is_termux_env() and get_active_usb_fd():
                            console.print("[bold cyan]🔌 Jika menggunakan kabel OTG, alamat bus USB berubah setelah reboot-bootloader.[/bold cyan]")
                            if Confirm.ask("[bold yellow]Re-bind perangkat OTG sekarang sebelum melanjutkan langkah berikutnya?[/bold yellow]", default=True):
                                status, usb_list = detect_otg_devices()
                                if status == "OK" and usb_list:
                                    console.print(f"[dim]Perangkat OTG terdeteksi: {usb_list[0]}. Jalankan ulang menu 12 setelah sesi terikat.[/dim]")
                                    bind_termux_usb_session(usb_list[0])
                        progress.start()
                else:
                    err_msg = res.stderr.strip().splitlines()[-1] if (res and res.stderr.strip()) else "Unknown error"
                    failed_steps.append((idx, f"{action} {target}", err_msg))
                    progress.stop()
                    console.print(f"[bold red]✖ Gagal pada langkah {idx} ({action} {target}): {err_msg}[/bold red]")
                    if not Confirm.ask("[bold red]Lanjutkan ke langkah berikutnya meskipun langkah ini gagal?[/bold red]", default=False):
                        break
                    progress.start()

    if failed_steps:
        console.print(Panel.fit(
            "[bold red]⚠ EKSEKUSI SCRIPT SELESAI DENGAN ERROR![/bold red]\n"
            "Auto-reboot DIBATALKAN demi mencegah perangkat terjebak bootloop/EDL.\n"
            "Periksa folder logs/ untuk melihat log stderr lengkap.",
            border_style="red"
        ))
    else:
        console.print(Panel.fit(
            f"[bold green]✔ Seluruh rangkaian '{parsed['script_name']}' berhasil dijalankan![/bold green]",
            border_style="green"
        ))
        has_final_reboot = any(op["action"] == "reboot" for op in parsed["operations"])
        if not simulation_mode and has_final_reboot:
            if Confirm.ask("[bold cyan]Script menyertakan perintah reboot akhir. Reboot perangkat ke sistem sekarang?[/bold cyan]", default=True):
                run_cmd(["fastboot", "reboot"])

# --- Modul 3: Recursive Firmware Loader ---
def get_file_checksum(filepath):
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                sha256.update(block)
        return sha256.hexdigest()
    except Exception:
        return "ERROR"

def load_firmware_info():
    firmware_dir = 'firmware'
    firmware_data = []
    if not os.path.exists(firmware_dir):
        return firmware_data

    for root, _, files in os.walk(firmware_dir):
        for filename in sorted(files):
            if filename.endswith(".img"):
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, firmware_dir)
                partition = os.path.splitext(filename)[0]
                raw_size = os.path.getsize(filepath)
                firmware_data.append({
                    "partition": partition,
                    "file": filename,
                    "rel_path": rel_path,
                    "full_path": filepath,
                    "size": format_size(raw_size),
                    "checksum": get_file_checksum(filepath)[:12] + "..."
                })
    return firmware_data

# --- Modul 4: Selective Flash Engine (Fastboot) ---
def start_flashing_engine(firmware_files, simulation_mode=True):
    if not simulation_mode and not has_existing_backups():
        console.print("[bold yellow]⚠ Belum ditemukan backup partisi krusial (persist/efs/modemst) di folder backups/.[/bold yellow]")
        if not Confirm.ask("[bold cyan]Lanjutkan tanpa backup partisi krusial? (Pilih 'n' untuk backup dulu via Menu 13)[/bold cyan]", default=True):
            return

    console.print("\n[dim]Masukkan nomor partisi dipisah koma (contoh: 1,3) atau ketik 'all' untuk semua.[/dim]")
    selection = Prompt.ask("[bold green]Partisi yang akan di-flash[/bold green]", default="all").strip().lower()

    if selection == "all":
        selected_queue = firmware_files
    else:
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",") if x.strip().isdigit()]
            selected_queue = [firmware_files[i] for i in indices if 0 <= i < len(firmware_files)]
        except Exception:
            selected_queue = []

    if not selected_queue:
        console.print("[bold red]✖ Pilihan partisi tidak valid atau kosong.[/bold red]")
        return

    has_vbmeta = any("vbmeta" in fw["partition"].lower() for fw in selected_queue)
    disable_verity = False
    if has_vbmeta:
        disable_verity = Confirm.ask(
            "[bold yellow]Terdeteksi partisi vbmeta. Tambahkan flag '--disable-verity --disable-verification'?[/bold yellow]",
            default=True
        )

    if not Confirm.ask(f"[bold red]⚠ Siap mem-flash {len(selected_queue)} partisi. Lanjutkan?[/bold red]"):
        return

    console.print(f"\n[bold yellow]⚡ Menginisialisasi Flash Engine ({'SIMULASI' if simulation_mode else 'REAL HARDWARE'})...[/bold yellow]")
    failed_partitions = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=35, style="magenta", complete_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        tasks = {fw["rel_path"]: progress.add_task(f"[cyan]Menunggu {fw['partition']}...", total=100) for fw in selected_queue}

        for fw in selected_queue:
            task_id = tasks[fw["rel_path"]]
            part = fw["partition"]
            filepath = fw["full_path"]

            if simulation_mode:
                while progress.tasks[task_id].completed < 100:
                    progress.update(task_id, advance=random.uniform(15, 30), description=f"[cyan]Simulasi flash {part}...")
                    time.sleep(0.1)
                progress.update(task_id, completed=100, description=f"[bold green]✔ {part} Sukses![/bold green]")
            else:
                cmd = ['fastboot', 'flash']
                if "vbmeta" in part.lower() and disable_verity:
                    cmd.extend(['--disable-verity', '--disable-verification'])
                cmd.extend([part, filepath])

                progress.update(task_id, advance=30, description=f"[cyan]Flashing {part}...[/cyan]")
                res = run_cmd(cmd)
                if res and res.returncode == 0:
                    progress.update(task_id, completed=100, description=f"[bold green]✔ {part} Sukses![/bold green]")
                else:
                    err_msg = res.stderr.strip().splitlines()[-1] if (res and res.stderr.strip()) else "Unknown error"
                    failed_partitions.append((part, err_msg))
                    progress.update(task_id, description=f"[bold red]✖ {part} GAGAL ({err_msg})[/bold red]")

    if failed_partitions:
        console.print(Panel.fit(
            "[bold red]⚠ PERINGATAN: Beberapa partisi GAGAL di-flash![/bold red]\n"
            "Auto-reboot DIBATALKAN demi mencegah bootloop/brick.\n"
            "Cek file di folder logs/ untuk detail error.",
            border_style="red"
        ))
    else:
        console.print(Panel.fit("[bold green]✔ Semua partisi berhasil di-flash![/bold green]", border_style="green"))
        if not simulation_mode and Confirm.ask("[bold cyan]Reboot perangkat ke sistem sekarang?[/bold cyan]"):
            run_cmd(['fastboot', 'reboot'])

# --- Modul 5: ADB Sideload ---
def start_adb_sideload(simulation_mode):
    archive_dir = 'archives'
    os.makedirs(archive_dir, exist_ok=True)
    zips = [f for f in os.listdir(archive_dir) if f.endswith('.zip')]

    if not zips:
        console.print(f"[bold red]✖ Tidak ada file .zip (OTA) di folder {archive_dir}/.[/bold red]")
        return

    table = Table(title="File OTA (.zip) Tersedia", style="magenta")
    table.add_column("No", style="dim", width=4)
    table.add_column("Nama File Sideload", style="cyan")
    table.add_column("Ukuran", justify="right", style="green")
    for idx, f in enumerate(zips):
        table.add_row(str(idx + 1), f, format_size(os.path.getsize(os.path.join(archive_dir, f))))
    console.print(table)

    pilihan = IntPrompt.ask("[bold green]Pilih file untuk Sideload (Ketik 0 batal)[/bold green]")
    if pilihan <= 0 or pilihan > len(zips):
        return

    selected_zip = os.path.join(archive_dir, zips[pilihan - 1])

    if simulation_mode:
        with console.status(f"[bold yellow]Menyimulasikan ADB Sideload {zips[pilihan-1]}...[/bold yellow]", spinner="dots"):
            time.sleep(2.5)
        console.print("[bold green]✔ Sideload selesai (Mode Simulasi)![/bold green]")
    else:
        console.print(f"\n[bold cyan]⚡ Memulai ADB Sideload: {zips[pilihan-1]}[/bold cyan]")
        with console.status("[bold magenta]Mengirim data OTA ke perangkat... (Jangan cabut kabel!)[/bold magenta]", spinner="bouncingBar"):
            res = run_cmd(['adb', 'sideload', selected_zip])
        if res and res.returncode == 0:
            console.print("[bold green]✔ ADB Sideload sukses![/bold green]")
        else:
            console.print(f"[bold red]✖ Sideload gagal:[/bold red]\n{res.stderr if res else 'Error eksekusi'}")

# --- Modul 6: Wipe Data & Reboot Manager ---
def wipe_device_data(simulation_mode):
    if simulation_mode:
        with console.status("[bold yellow]Menyimulasikan Wipe Data...[/bold yellow]", spinner="dots"):
            time.sleep(1.5)
        console.print("[bold green]✔ Data berhasil dihapus (Mode Simulasi)![/bold green]")
    else:
        with console.status("[bold red]Menghapus data (fastboot -w)...[/bold red]", spinner="bouncingBar"):
            res = run_cmd(['fastboot', '-w'])
        if res and res.returncode == 0:
            console.print("[bold green]✔ Factory Reset berhasil![/bold green]")
        else:
            console.print(f"[bold red]✖ Gagal melakukan Wipe Data: {res.stderr if res else ''}[/bold red]")

def reboot_manager(simulation_mode, device_mode):
    console.print("\n[bold cyan]=== REBOOT & SLOT MANAGER ===[/bold cyan]")
    console.print("1. Reboot ke System (Normal)")
    console.print("2. Reboot ke Bootloader / Fastboot")
    console.print("3. Reboot ke FastbootD (Userspace Fastboot)")
    console.print("4. Reboot ke Recovery")
    console.print("5. Ganti Slot Aktif A/B (Fastboot Only)")
    console.print("0. Batal")

    pilihan = IntPrompt.ask("[bold green]Pilih target[/bold green]", choices=["0", "1", "2", "3", "4", "5"])
    if pilihan == 0:
        return

    if simulation_mode:
        console.print(f"[bold green]✔ Perintah Reboot opsi {pilihan} dieksekusi (Mode Simulasi).[/bold green]")
        return

    if pilihan == 5:
        slot = Prompt.ask("Pilih slot target", choices=["a", "b"])
        res = run_cmd(['fastboot', f'--set-active={slot}'])
        console.print(f"[green]{res.stderr if res else 'Selesai'}[/green]")
        return

    cmd_map = {
        1: ['fastboot', 'reboot'] if device_mode == 'fastboot' else ['adb', 'reboot'],
        2: ['fastboot', 'reboot-bootloader'] if device_mode == 'fastboot' else ['adb', 'reboot', 'bootloader'],
        3: ['fastboot', 'reboot', 'fastboot'] if device_mode == 'fastboot' else ['adb', 'reboot', 'fastboot'],
        4: ['fastboot', 'reboot', 'recovery'] if device_mode == 'fastboot' else ['adb', 'reboot', 'recovery'],
    }
    res = run_cmd(cmd_map[pilihan])
    if res and res.returncode == 0:
        console.print("[bold green]✔ Perintah reboot dikirim! (Jika pakai OTG, lakukan Re-Bind USB di menu 11 setelah HP menyala ulang)[/bold green]")
    else:
        console.print(f"[bold red]✖ Gagal reboot: {res.stderr if res else ''}[/bold red]")

# --- Main Program ---
def main():
    console.clear()
    log_file = setup_logger()
    simulation_mode = True
    device_mode = None
    firmware_files = []

    if get_active_usb_fd():
        devices, detected_mode = scan_devices(auto_prompt_otg=False)
        if devices != "NOT_INSTALLED" and detected_mode:
            simulation_mode = False
            device_mode = detected_mode

    while True:
        usb_fd = get_active_usb_fd()
        otg_badge = f"[bold green]OTG BOUND (FD:{usb_fd})[/bold green]" if usb_fd else "[dim]NO OTG FD[/dim]"
        status_badge = "[bold yellow]SIMULATION MODE[/bold yellow]" if simulation_mode else f"[bold green]HARDWARE ({device_mode.upper()})[/bold green]"

        console.print(Panel.fit(
            f"[bold cyan]FlashLab v2.4[/bold cyan] | Status: {status_badge} | USB: {otg_badge}\n"
            f"[dim]Log aktif: {log_file}[/dim]",
            title="Termux Android Flasher, Payload Dumper & Backup Suite",
            border_style="blue"
        ))

        console.print("1.  [cyan]Scan Devices (Fastboot, ADB & Auto-OTG)[/cyan]")
        console.print("2.  [cyan]Cek Info Perangkat (Unlock Status, ARB & Slot)[/cyan]")
        console.print("3.  [blue]Wireless ADB (Pair / Connect Wi-Fi)[/blue]")
        console.print("4.  [magenta]Ekstrak ROM (.zip / .tgz)[/magenta]")
        console.print("5.  [cyan]Load Firmware (Scan Rekursif .img)[/cyan]")
        console.print("6.  [magenta]Flash Firmware (Selective Fastboot)[/magenta]")
        console.print("7.  [cyan]ADB Sideload (OTA Update)[/cyan]")
        console.print("8.  [yellow]Wipe Data (Factory Reset)[/yellow]")
        console.print("9.  [green]Reboot & Slot Manager[/green]")
        console.print("10. [magenta]Ekstrak Payload.bin (OTA CrAU Dumper)[/magenta]")
        console.print("11. [yellow]Bind / Refresh OTG USB (termux-usb Non-Root)[/yellow]")
        console.print("12. [green]Auto-Flash via flash_all.sh (Xiaomi / Pixel + ARB Check)[/green]")
        console.print("13. [bold cyan]Backup Partisi Krusial (Persist, EFS, Modemst1/2 via ADB)[/bold cyan] [bold yellow]<- BARU[/bold yellow]")
        console.print("0.  [red]Keluar / Exit[/red]")

        pilihan = IntPrompt.ask("\n[bold green]Pilih menu[/bold green]", choices=[str(i) for i in range(0, 14)])

        if pilihan == 1:
            console.print("\n[bold yellow]Mencari perangkat yang terhubung...[/bold yellow]")
            devices, detected_mode = scan_devices(auto_prompt_otg=True)
            if devices == "NOT_INSTALLED":
                console.print("[bold red]✖ Paket 'android-tools' tidak ditemukan! Jalankan: pkg install android-tools[/bold red]")
                simulation_mode = True
                device_mode = None
            elif not devices:
                console.print("[dim yellow]⚠ Tidak ada perangkat terdeteksi. Beralih ke Mode Simulasi...[/dim yellow]")
                simulation_mode = True
                device_mode = None
            else:
                for d in devices:
                    console.print(f"[bold green]✔ Terdeteksi:[/bold green] {d}")
                if detected_mode:
                    simulation_mode = False
                    device_mode = detected_mode
                else:
                    console.print("[bold yellow]⚠ Perangkat terdeteksi tapi belum siap (Unauthorized/Offline). Tetap di Mode Simulasi.[/bold yellow]")
                    simulation_mode = True

        elif pilihan == 2:
            inspect_device(simulation_mode, device_mode)

        elif pilihan == 3:
            wireless_adb_menu()

        elif pilihan == 4:
            extract_firmware()

        elif pilihan == 5:
            console.print("\n[bold cyan]Memindai file .img secara rekursif di firmware/...[/bold cyan]")
            firmware_files = load_firmware_info()
            if not firmware_files:
                console.print("[bold red]✖ Tidak ada file .img ditemukan di folder firmware/.[/bold red]")
            else:
                table = Table(title=f"Ditemukan {len(firmware_files)} File Partisi", style="magenta")
                table.add_column("No", style="dim", width=4)
                table.add_column("Partisi", style="bold cyan")
                table.add_column("Lokasi File", style="white")
                table.add_column("Ukuran", justify="right", style="green")
                table.add_column("SHA-256", style="yellow")
                for idx, fw in enumerate(firmware_files, 1):
                    table.add_row(str(idx), fw["partition"], fw["rel_path"], fw["size"], fw["checksum"])
                console.print(table)

        elif pilihan == 6:
            if not firmware_files:
                console.print("[bold red]✖ Kamu belum memuat firmware! Jalankan menu 5 terlebih dahulu.[/bold red]")
            else:
                start_flashing_engine(firmware_files, simulation_mode)

        elif pilihan == 7:
            start_adb_sideload(simulation_mode)

        elif pilihan == 8:
            if Confirm.ask("[bold red]⚠ Lanjutkan Factory Reset (Wipe Data)?[/bold red]"):
                wipe_device_data(simulation_mode)

        elif pilihan == 9:
            reboot_manager(simulation_mode, device_mode)

        elif pilihan == 10:
            payload_bin_extractor_menu()

        elif pilihan == 11:
            otg_usb_manager()

        elif pilihan == 12:
            flash_all_script_menu(simulation_mode, device_mode)

        elif pilihan == 13:
            adb_critical_backup_menu(simulation_mode, device_mode)

        elif pilihan == 0:
            console.print("\n[bold green]Terima kasih telah menggunakan FlashLab v2.4![/bold green]\n")
            break

if __name__ == "__main__":
    main()
