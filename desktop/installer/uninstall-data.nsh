; Uninstall offers to keep or remove the person's data: cameras, incidents, evidence, accounts.
; The default, including for a silent uninstall (/S), is to KEEP it. Deleting evidence is not a
; thing an uninstaller should do unasked.
!macro customUnInstall
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also remove your VIGIL-88 data (cameras, zones, incidents, evidence and accounts)?$\r$\n$\r$\nChoose No to keep it for a later install. This cannot be undone." /SD IDNO IDNO vigilKeepData
    RMDir /r "$APPDATA\${PRODUCT_FILENAME}"
  vigilKeepData:
!macroend
