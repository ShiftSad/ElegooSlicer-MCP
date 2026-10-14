# Click inside ONE window of one process (test automation for the test app only).
# -X/-Y are pixels from the window's top-left corner, as in the PNG tools\remote-api\winshot.ps1 writes
# for that window. -Title picks the window whose title contains that text (default: the
# largest visible window of the process). Brings the window to the front first.
# Usage: pwsh tools\remote-api\winclick.ps1 -ProcessId 1234 -X 100 -Y 200 [-Title "Print"] [-Double]
param([int]$ProcessId, [int]$X, [int]$Y, [string]$Title = "", [switch]$Double)

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class WinClick {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint f, IntPtr e);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public static List<IntPtr> Windows(uint pid) {
        var list = new List<IntPtr>();
        EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
            if (p == pid && IsWindowVisible(h)) list.Add(h); return true; }, IntPtr.Zero);
        return list;
    }
    public static string Title(IntPtr h) { var s = new StringBuilder(512); GetWindowText(h, s, 512); return s.ToString(); }
}
"@

$best = [IntPtr]::Zero; $bestArea = 0; $bestRect = $null
foreach ($h in [WinClick]::Windows([uint32]$ProcessId)) {
    $r = New-Object WinClick+RECT
    [void][WinClick]::GetWindowRect($h, [ref]$r)
    $area = ($r.R - $r.L) * ($r.B - $r.T)
    if ($Title -and -not ([WinClick]::Title($h)).Contains($Title)) { continue }
    if ($area -gt $bestArea) { $best = $h; $bestArea = $area; $bestRect = $r }
}
if ($best -eq [IntPtr]::Zero) { Write-Error "no window"; exit 1 }

# An ALT tap lets a background process move another window to the front.
[WinClick]::keybd_event(0x12, 0, 0, [IntPtr]::Zero); [WinClick]::keybd_event(0x12, 0, 2, [IntPtr]::Zero)
[void][WinClick]::SetForegroundWindow($best)
Start-Sleep -Milliseconds 300
$sx = $bestRect.L + $X; $sy = $bestRect.T + $Y
[void][WinClick]::SetCursorPos($sx, $sy)
Start-Sleep -Milliseconds 100
$n = if ($Double) { 2 } else { 1 }
for ($i = 0; $i -lt $n; $i++) {
    [WinClick]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)   # left down
    [WinClick]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)   # left up
    Start-Sleep -Milliseconds 80
}
"clicked '{0}' at {1},{2} (screen {3},{4})" -f [WinClick]::Title($best), $X, $Y, $sx, $sy
