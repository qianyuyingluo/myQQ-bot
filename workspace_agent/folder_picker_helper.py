# Adapted from LabAI/backend/app/services/folder_picker_helper.py
"""Standalone native-dialog helper; stdout is a single UTF-8 JSON response."""
from __future__ import annotations

import json
import os
import sys


def _windows_directory(initial: str | None) -> str | None:
    """Use the modern Shell picker without an invisible Tk owner window."""
    # Vtable layout: Microsoft's WinSDK ShObjIdl_core.h (IFileDialog and
    # IShellItem); Show accepts NULL ownership and returns 0x800704C7 on cancel.
    import ctypes
    from ctypes import wintypes
    from uuid import UUID

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def guid(value):
        return GUID.from_buffer_copy(UUID(value).bytes_le)

    def check(hr):
        if hr < 0:
            raise OSError(f"Folder dialog failed: HRESULT 0x{hr & 0xffffffff:08x}")

    def invoke(pointer, index, argtypes, *args):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])
        return method(pointer, *args)

    shell = ctypes.WinDLL("shell32")
    # WinDLL preserves HRESULT values rather than raising before we can classify
    # ERROR_CANCELLED as a normal user action.
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoUninitialize.argtypes = []
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                 ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = ctypes.c_long

    check(ole.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
    dialog, initial_item, selected_item = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    selected_name = ctypes.c_void_p()
    try:
        clsid = guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid_dialog = guid("D57C7288-D4AD-4768-BE02-9D969532D960")
        iid_item = guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")
        check(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_dialog), ctypes.byref(dialog)))
        # Preserve Shell defaults and add PICKFOLDERS, FORCEFILESYSTEM,
        # PATHMUSTEXIST and NOCHANGEDIR (as required by SetOptions).
        options = wintypes.DWORD()
        check(invoke(dialog, 10, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options)))
        check(invoke(dialog, 9, [wintypes.DWORD], options.value | 0x20 | 0x40 | 0x800 | 0x8))
        check(invoke(dialog, 17, [wintypes.LPCWSTR], "樱洛 Agent - 选择工作文件夹"))
        if initial:
            hr = shell.SHCreateItemFromParsingName(initial, None, ctypes.byref(iid_item), ctypes.byref(initial_item))
            if hr >= 0:
                check(invoke(dialog, 12, [ctypes.c_void_p], initial_item))
        hr = invoke(dialog, 3, [wintypes.HWND], None)
        if (hr & 0xffffffff) == 0x800704C7:
            return None
        check(hr)
        check(invoke(dialog, 20, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(selected_item)))
        check(invoke(selected_item, 5, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                     0x80058000, ctypes.byref(selected_name)))  # SIGDN_FILESYSPATH
        return ctypes.wstring_at(selected_name)
    finally:
        if selected_name:
            ole.CoTaskMemFree(selected_name)
        for pointer in (selected_item, initial_item, dialog):
            if pointer:
                invoke(pointer, 2, [])
        ole.CoUninitialize()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    root = None
    try:
        initial = sys.argv[1] if len(sys.argv) > 1 else None
        if os.name == "nt":
            selected = _windows_directory(initial)
        else:
            import tkinter
            from tkinter import filedialog

            root = tkinter.Tk()
            root.title("樱洛 Agent - 选择工作文件夹")
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            selected = filedialog.askdirectory(
                parent=root, title="樱洛 Agent - 选择工作文件夹",
                initialdir=initial, mustexist=True,
            )
        print(json.dumps({"path": str(selected) if selected else None}, ensure_ascii=False), flush=True)
        return 0
    except Exception:
        print(json.dumps({"error": "Native folder dialog is unavailable."}), flush=True)
        return 1
    finally:
        if root is not None:
            root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
