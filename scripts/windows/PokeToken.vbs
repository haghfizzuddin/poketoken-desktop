' PokeToken — open or close the window from Windows with no console flash.
' Double-click it, or right-click > Send to > Desktop (create shortcut) and pin that to the taskbar.
' Requires WSL2 with WSLg and `scripts/install.sh` run inside the distro. Edit DISTRO if needed.
Const DISTRO = "Ubuntu"
Set sh = CreateObject("WScript.Shell")
sh.Run "wsl.exe -d " & DISTRO & " -- bash -lc ""$HOME/.local/bin/poketoken toggle""", 0, False
