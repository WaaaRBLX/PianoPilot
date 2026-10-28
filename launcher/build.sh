#!/bin/sh
# Builds PianoPilot.exe from launcher.c with clang + LLVM's Windows tools (no Visual Studio needed).
# Run from this folder: sh build.sh   ->  ../PianoPilot.exe
set -e
cd "$(dirname "$0")"
OUT=$(mktemp -d)
for lib in kernel32 user32 shell32; do
  llvm-dlltool -m i386:x86-64 -d $lib.def -l "$OUT/$lib.lib"
done
llvm-rc /nologo /fo "$OUT/launcher.res" launcher.rc
clang --target=x86_64-pc-windows-msvc -Os -ffreestanding -fno-builtin -fno-stack-protector \
      -mno-stack-arg-probe -fno-asynchronous-unwind-tables -Wall -Wextra -Werror \
      -c launcher.c -o "$OUT/launcher.obj"
lld-link /nologo /out:../PianoPilot.exe /subsystem:windows /entry:start /nodefaultlib /manifest:no \
         /release /dynamicbase /nxcompat /highentropyva /opt:ref \
         "$OUT/launcher.obj" "$OUT/launcher.res" "$OUT/kernel32.lib" "$OUT/user32.lib" "$OUT/shell32.lib"
rm -rf "$OUT"
echo "Built ../PianoPilot.exe"
