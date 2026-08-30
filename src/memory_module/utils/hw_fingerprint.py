'''
Hardware Fingerprint — cross-platform machine identification.

Generates a deterministic, strict HW fingerprint for encrypting biomem state
exports and tamper-resistant storage. The fingerprint is derived from physical
hardware identifiers that survive an OS reinstall.

Primary sources (per platform):
  - Windows: WMI Win32_BaseBoard (SerialNumber, Manufacturer, Product)
  - Linux:   /sys/class/dmi/id/ (board_serial, board_vendor, board_name)
  - macOS:   IOKit hardware UUID

Fallback:
  If the primary source fails (virtual machine, container, missing permissions),
  a combination of platform.node() + uuid.getnode() + os.getlogin() is used.
  This combination is less stable (changes when the PC is renamed), but ensures
  functionality on 100% of platforms.
'''
import hashlib
import logging
import platform
import uuid
import os
from typing import Optional

logger = logging.getLogger('bdbm.hw_fingerprint')

_FINGERPRINT_LENGTH = 32


def get_hw_fingerprint() -> bytes:
    '''
    Returns a deterministic HW fingerprint of the current machine.

    Returns:
        bytes: 32 bytes (SHA-256 hash) unique to the given hardware.
    '''
    raw = _collect_primary_identifiers()
    if not raw:
        raw = _collect_fallback_identifiers()
        logger.warning('HW Fingerprint: primary identifiers unavailable, using fallback (hostname + MAC + username).')

    raw = raw.strip().lower()
    return hashlib.sha256(raw.encode('utf-8')).digest()


def get_hw_fingerprint_hex() -> str:
    '''Returns the HW fingerprint as a hex string (64 characters).'''
    return get_hw_fingerprint().hex()


def _collect_primary_identifiers() -> Optional[str]:
    '''
    Attempts to read strict HW identifiers per platform.

    Returns:
        str with the identifiers, or None on failure.
    '''
    system = platform.system()
    if system == 'Windows':
        return _windows_wmi_baseboard()
    if system == 'Linux':
        return _linux_dmi_baseboard()
    if system == 'Darwin':
        return _macos_hardware_uuid()

    logger.debug(f"HW Fingerprint: unknown platform '{system}'")
    return None


def _windows_wmi_baseboard() -> Optional[str]:
    '''
    Windows: Reads identifiers from WMI Win32_BaseBoard.

    Uses subprocess + wmic (available on all Windows versions)
    instead of the pythoncom/WMI library — removes the pywin32 dependency.
    '''
    import subprocess
    try:
        result = subprocess.run(
            ['wmic', 'baseboard', 'get', 'SerialNumber,Manufacturer,Product', '/format:csv'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 134217728),
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            if len(lines) >= 2:
                data_line = lines[-1]
                parts = data_line.split(',')
                if len(parts) >= 4:
                    identifier = '|'.join(parts[1:])
                    if identifier and identifier != '||':
                        logger.debug('HW Fingerprint: Windows WMI baseboard OK')
                        return f'win_baseboard|{identifier}'
    except FileNotFoundError:
        return _windows_powershell_baseboard()
    except Exception as e:
        logger.debug(f'HW Fingerprint: Windows WMI selhalo: {e}')

    return _windows_powershell_baseboard()


def _windows_powershell_baseboard() -> Optional[str]:
    '''Windows fallback — PowerShell Get-CimInstance.'''
    import subprocess
    cmd = 'Get-CimInstance Win32_BaseBoard | Select-Object -Property SerialNumber,Manufacturer,Product | ConvertTo-Csv -NoTypeInformation'
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 134217728),
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [l.strip().strip('"') for l in result.stdout.strip().splitlines() if l.strip()]
            if len(lines) >= 2:
                identifier = lines[-1].replace('"', '').strip()
                if identifier:
                    logger.debug('HW Fingerprint: Windows PowerShell baseboard OK')
                    return f'win_baseboard|{identifier}'
    except Exception as e:
        logger.debug(f'HW Fingerprint: Windows PowerShell selhalo: {e}')

    return None


def _linux_dmi_baseboard() -> Optional[str]:
    '''
    Linux: Reads /sys/class/dmi/id/ identifiers.

    Access does not require root on most distributions (world-readable).
    '''
    dmi_path = '/sys/class/dmi/id'
    fields = ['board_serial', 'board_vendor', 'board_name']

    parts = []
    for field in fields:
        try:
            filepath = os.path.join(dmi_path, field)
            if os.path.isfile(filepath):
                with open(filepath, 'r') as f:
                    value = f.read().strip()
                    if value and value != 'None' and value != 'Default string':
                        parts.append(value)
        except (PermissionError, OSError) as e:
            logger.debug(f'HW Fingerprint: Linux DMI {field} unavailable: {e}')

    if parts:
        identifier = '|'.join(parts)
        logger.debug('HW Fingerprint: Linux DMI baseboard OK')
        return f'linux_baseboard|{identifier}'

    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            if os.path.isfile(path):
                with open(path, 'r') as f:
                    machine_id = f.read().strip()
                    if machine_id:
                        logger.debug(f'HW Fingerprint: Linux machine-id z {path}')
                        return f'linux_machine_id|{machine_id}'
        except (PermissionError, OSError):
            pass

    return None


def _macos_hardware_uuid() -> Optional[str]:
    '''macOS: IOKit Hardware UUID.'''
    import subprocess
    try:
        result = subprocess.run(
            ['system_profiler', 'SPHardwareDataType'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if 'Hardware UUID' in line:
                    hw_uuid = line.split(':')[-1].strip()
                    if hw_uuid:
                        logger.debug('HW Fingerprint: macOS Hardware UUID OK')
                        return f'macos_uuid|{hw_uuid}'
    except Exception as e:
        logger.debug(f'HW Fingerprint: macOS selhalo: {e}')

    return None


def _collect_fallback_identifiers() -> str:
    '''
    Fallback identifiers — always available, but less stable.

    Combination of hostname + MAC address + username.
    '''
    hostname = platform.node()
    mac = str(uuid.getnode())
    try:
        username = os.getlogin()
    except OSError:
        username = os.environ.get('USER', os.environ.get('USERNAME', 'unknown'))

    return f'fallback|{hostname}|{mac}|{username}'
