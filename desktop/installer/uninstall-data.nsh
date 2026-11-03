; Uninstall offers to keep or remove the person's data: cameras, incidents, evidence, accounts.
; The default, including for a silent uninstall (/S), is to KEEP it. Deleting evidence is not a
; thing an uninstaller should do unasked.
;
; The one way to remove it without a prompt is to say so: pass /REMOVEDATA on the uninstaller's
; command line. That exists so the "remove my data" branch can be exercised by a script, and for
; an administrator who scripts an uninstall on purpose. /S alone never implies it.
!macro customUnInstall
  Push $R1
  Push $R2
  Push $R3
  StrLen $R1 "$CMDLINE"
  StrCpy $R3 0
  vigilScan:
    IntCmp $R3 $R1 vigilAsk vigilLook vigilAsk
  vigilLook:
    StrCpy $R2 "$CMDLINE" 11 $R3
    StrCmp $R2 "/REMOVEDATA" vigilRemove
    IntOp $R3 $R3 + 1
    Goto vigilScan
  vigilAsk:
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also remove your VIGIL-88 data (cameras, zones, incidents, evidence and accounts)?$\r$\n$\r$\nChoose No to keep it for a later install. This cannot be undone." /SD IDNO IDNO vigilKeepData
  vigilRemove:
    RMDir /r "$APPDATA\${PRODUCT_FILENAME}"
  vigilKeepData:
  Pop $R3
  Pop $R2
  Pop $R1
!macroend
