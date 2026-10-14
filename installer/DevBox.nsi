;===============================================================================
; 开发环境管理器 Windows 安装脚本（NSIS 3.x）
; 生成带 Authenticode 数字签名的 Setup.exe 安装包
;===============================================================================
!include "MUI2.nsh"
!include "WinVer.nsh"

Name "开发环境管理器"
OutFile "..\dist\DevBox-Setup.exe"

; --- 基本设置 ---
SetCompressor /SOLID lzma

!define WIN_ICON "devbox.ico"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DevBox"
!if /FILEexists "${WIN_ICON}"
  Icon "${WIN_ICON}"
  UninstallIcon "${WIN_ICON}"
  !define MUI_ICON "${WIN_ICON}"
  !define MUI_UNICON "${WIN_ICON}"
!endif

; --- 安装目录 ---
InstallDir "$PROGRAMFILES64\DevBox"
InstallDirRegKey HKLM "Software\DevBox" "InstallDir"
RequestExecutionLevel admin

; --- 版本信息（文件属性） ---
VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName"      "开发环境管理器"
VIAddVersionKey "ProductVersion"   "1.0.0"
VIAddVersionKey "FileVersion"      "1.0.0"
VIAddVersionKey "FileDescription"   "开发环境管理器 - Windows 多版本开发环境管理器"
VIAddVersionKey "CompanyName"      "子柒"
VIAddVersionKey "LegalCopyright"   "© 2026 子柒"
VIAddVersionKey "OriginalFilename"  "DevBox-Setup.exe"

; --- MUI2 界面 ---
!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "您确定要退出开发环境管理器的安装吗？"

; --- 欢迎页 ---
!define MUI_WELCOMEPAGE_TITLE "欢迎使用开发环境管理器安装向导"
!define MUI_WELCOMEPAGE_TEXT "本向导将引导您完成开发环境管理器的安装。\r\n\r\n开发环境管理器是一个 Windows 多版本开发环境管理工具，支持 Python、Node.js、Java 等多种运行时环境的安装与切换管理。\r\n\r\n版本 1.0.0 · PolyForm Noncommercial 许可 · © 2026 子柒\r\n\r\n点击“下一步”继续，或点击“取消”退出安装。"

; --- 安装页 ---
!define MUI_INSTFILESPAGE_ABORTHEADER_TEXT "安装已中止"
!define MUI_INSTFILESPAGE_ABORTHEADER_SUBTEXT "开发环境管理器未完成安装，您可以稍后重新运行安装程序。"
!define MUI_INSTFILESPAGE_FINISHHEADER_TEXT "安装完成"
!define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT "开发环境管理器已成功安装到您的计算机。"

; --- 完成页 ---
!define MUI_FINISHPAGE_TITLE "安装完成"
!define MUI_FINISHPAGE_TEXT "开发环境管理器已成功安装到您的计算机。\r\n\r\n点击“完成”退出安装向导。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\DevBox.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动开发环境管理器"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\README.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "查看使用说明"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

;===============================================================================
; 主安装 Section
;===============================================================================
Section "开发环境管理器" SecMain
    SetRegView 64
    SetOutPath "$INSTDIR"

    ; 复制程序文件
    File /r "..\dist\DevBox\*"
    ; 复制图标文件（快捷方式引用）
    File "devbox.ico"

    WriteRegStr HKLM "Software\DevBox" "InstallDir" "$INSTDIR"
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; 写入「设置 → 应用」/「控制面板 → 程序和功能」卸载入口
    ; （NSIS 默认不写这些键，缺失会导致系统里看不到本程序、无法从设置卸载）
    WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "开发环境管理器"
    WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "1.0.0"
    WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "子柒"
    WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKLM "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
    WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\DevBox.exe,0"
    WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
    WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1

    ; 创建桌面快捷方式（名称为"开发环境管理器"，带图标）
    CreateShortcut "$DESKTOP\开发环境管理器.lnk" \
                   "$INSTDIR\DevBox.exe" \
                   "" \
                   "$INSTDIR\devbox.ico" \
                   0

    ; 创建开始菜单快捷方式
    CreateDirectory "$SMPROGRAMS\开发环境管理器"
    CreateShortcut "$SMPROGRAMS\开发环境管理器\开发环境管理器.lnk" \
                   "$INSTDIR\DevBox.exe" \
                   "" \
                   "$INSTDIR\devbox.ico" \
                   0
    CreateShortcut "$SMPROGRAMS\开发环境管理器\卸载.lnk" \
                   "$INSTDIR\Uninstall.exe" \
                   "" \
                   "$INSTDIR\devbox.ico" \
                   0
SectionEnd

;===============================================================================
; 卸载 Section
;===============================================================================
Section "Uninstall"
    SetRegView 64
    ; 停止运行中的进程（若有）
    ExecWait 'taskkill /F /IM DevBox.exe'
    Sleep 300

    ; 删除快捷方式
    Delete "$DESKTOP\开发环境管理器.lnk"
    Delete "$SMPROGRAMS\开发环境管理器\开发环境管理器.lnk"
    Delete "$SMPROGRAMS\开发环境管理器\卸载.lnk"
    RMDir  "$SMPROGRAMS\开发环境管理器"

    ; 删除程序文件
    Delete "$INSTDIR\Uninstall.exe"
    Delete "$INSTDIR\devbox.ico"
    DeleteRegKey HKLM "Software\DevBox"
    DeleteRegKey HKLM "${UNINST_KEY}"

    RMDir /r "$INSTDIR"
SectionEnd
