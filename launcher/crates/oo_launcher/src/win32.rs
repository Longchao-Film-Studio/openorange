//! Win32 原生控件薄封装。全部使用系统标准控件类（EDIT / BUTTON / STATIC）。

use windows::core::{PCWSTR, Result as WinResult};
use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::Graphics::Gdi::{GetStockObject, DEFAULT_GUI_FONT, HFONT};
use windows::Win32::UI::Controls::EM_SETLIMITTEXT;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, GetWindowTextLengthW, GetWindowTextW, SendMessageW, SetWindowTextW,
    ES_AUTOHSCROLL, ES_PASSWORD, HMENU, WINDOW_EX_STYLE, WINDOW_STYLE, WS_BORDER, WS_CHILD,
    WS_TABSTOP, WS_VISIBLE,
};

// 控件样式常量（windows crate 未导出全部 BS_*/WM_ 值）
const BS_PUSHBUTTON: u32 = 0x0000_0000;
const BS_AUTOCHECKBOX: u32 = 0x0000_0003;

/// WS_* 是 WINDOW_STYLE、ES_* 是 i32，混用时统一折算为 u32 再包一层。
const fn ws(v: WINDOW_STYLE) -> u32 { v.0 }
const fn es(v: i32) -> u32 { v as u32 }
const WM_GETTEXT: u32 = 0x000D;
const WM_SETTEXT: u32 = 0x000C;

pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub struct Controls {
    pub font: HFONT,
}

impl Default for Controls {
    fn default() -> Self { Self::new() }
}

impl Controls {
    pub fn new() -> Self {
        let font = unsafe { HFONT(GetStockObject(DEFAULT_GUI_FONT).0) };
        Controls { font }
    }

    fn make(
        &self,
        class: PCWSTR,
        text: &str,
        style: WINDOW_STYLE,
        parent: HWND,
        id: i32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
    ) -> WinResult<HWND> {
        let wtext = wide(text);
        unsafe {
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE(0),
                class,
                PCWSTR(wtext.as_ptr()),
                style,
                x,
                y,
                w,
                h,
                Some(parent),
                Some(HMENU(id as isize as *mut core::ffi::c_void)),
                None,
                None,
            )?;
            let _ = SendMessageW(hwnd, 0x0030 /* WM_SETFONT */, Some(WPARAM(self.font.0 as usize)), Some(LPARAM(1)));
            Ok(hwnd)
        }
    }

    pub fn label(&self, parent: HWND, text: &str, id: i32, x: i32, y: i32, w: i32, h: i32) -> WinResult<HWND> {
        self.make(
            windows::core::w!("STATIC"),
            text,
            WS_CHILD | WS_VISIBLE,
            parent,
            id,
            x,
            y,
            w,
            h,
        )
    }

    pub fn edit(
        &self,
        parent: HWND,
        text: &str,
        id: i32,
        x: i32,
        y: i32,
        w: i32,
        h: i32,
        password: bool,
    ) -> WinResult<HWND> {
        let mut style: u32 =
            ws(WS_CHILD) | ws(WS_VISIBLE) | ws(WS_BORDER) | ws(WS_TABSTOP) | es(ES_AUTOHSCROLL);
        if password {
            style |= es(ES_PASSWORD);
        }
        let hwnd = self.make(windows::core::w!("EDIT"), text, WINDOW_STYLE(style), parent, id, x, y, w, h)?;
        unsafe {
            let _ = SendMessageW(hwnd, EM_SETLIMITTEXT, Some(WPARAM(256)), Some(LPARAM(0)));
        }
        Ok(hwnd)
    }

    pub fn button(&self, parent: HWND, text: &str, id: i32, x: i32, y: i32, w: i32, h: i32) -> WinResult<HWND> {
        self.make(
            windows::core::w!("BUTTON"),
            text,
            WINDOW_STYLE(ws(WS_CHILD) | ws(WS_VISIBLE) | ws(WS_TABSTOP) | BS_PUSHBUTTON),
            parent,
            id,
            x,
            y,
            w,
            h,
        )
    }

    pub fn checkbox(&self, parent: HWND, text: &str, id: i32, x: i32, y: i32, w: i32, h: i32) -> WinResult<HWND> {
        self.make(
            windows::core::w!("BUTTON"),
            text,
            WINDOW_STYLE(ws(WS_CHILD) | ws(WS_VISIBLE) | ws(WS_TABSTOP) | BS_AUTOCHECKBOX),
            parent,
            id,
            x,
            y,
            w,
            h,
        )
    }
}

/// 读取控件文本。
pub fn get_text(hwnd: HWND) -> String {
    unsafe {
        let len = GetWindowTextLengthW(hwnd);
        if len <= 0 {
            return String::new();
        }
        let mut buf = vec![0u16; len as usize + 1];
        let n = GetWindowTextW(hwnd, &mut buf);
        String::from_utf16_lossy(&buf[..n as usize])
    }
}

pub fn set_text(hwnd: HWND, s: &str) {
    let w = wide(s);
    unsafe {
        let _ = SetWindowTextW(hwnd, PCWSTR(w.as_ptr()));
    }
}

/// 读取复选框勾选状态（BM_GETCHECK = 0x00F0）。
pub fn get_check(hwnd: HWND) -> bool {
    unsafe {
        let r = SendMessageW(hwnd, 0x00F0, Some(WPARAM(0)), Some(LPARAM(0)));
        r.0 == 1 // BST_CHECKED
    }
}

/// 设置复选框勾选状态（BM_SETCHECK = 0x00F1）。
pub fn set_check(hwnd: HWND, checked: bool) {
    unsafe {
        let _ = SendMessageW(hwnd, 0x00F1,
                              Some(WPARAM(if checked { 1 } else { 0 })),
                              Some(LPARAM(0)));
    }
}

#[allow(dead_code)]
pub fn get_text_msg(hwnd: HWND) -> String {
    unsafe {
        let mut buf = [0u16; 512];
        let n = SendMessageW(hwnd, WM_GETTEXT, Some(WPARAM(buf.len())), Some(LPARAM(buf.as_mut_ptr() as isize)));
        String::from_utf16_lossy(&buf[..n.0 as usize])
    }
}

#[allow(dead_code)]
pub fn set_text_msg(hwnd: HWND, s: &str) {
    let w = wide(s);
    unsafe {
        let _ = SendMessageW(hwnd, WM_SETTEXT, Some(WPARAM(0)), Some(LPARAM(w.as_ptr() as isize)));
    }
}
