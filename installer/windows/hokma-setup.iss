; Hokma-Setup.exe: one-click installer for Windows (Inno Setup 6.3 or newer). SPEC-001 addendum 0.0.13, task 3138.
;
; Built by .github/workflows/release-installers.yml:
;   iscc /DAppVersion=0.0.15 /DSourceZip=<abs>\hokma-src.zip /DOutputDir=<abs>\dist
;        [/DAppExe=<abs>\Hokma.exe] [/DWebView2Setup=<abs>\MicrosoftEdgeWebview2Setup.exe] [/DSignTool] hokma-setup.iss
;
; What it does: the wizard asks only the folder (and, with a previous install, Update or Reinstall from scratch);
; every stage runs hidden in setup-helper.ps1, which reuses install.ps1 and uninstall.ps1. No console, no admin.
; The directory page is the student's WORKING folder ({app}); program files go to {localappdata}\Programs\Hokma.
;
; Command line (CI and the desktop app): /VERYSILENT /MODE=fresh|update|reinstall /DIR=<folder> /WAITPID=<pid>
;   /NOCLAUDE (do not install Claude Code). With /WAITPID the app is opened again at the end, even when silent.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceZip
  #error Pass /DSourceZip=<absolute path of hokma-src.zip>
#endif
#ifndef OutputDir
  #define OutputDir "..\..\build\installer"
#endif
#define AppFolder "{localappdata}\Programs\Hokma"

[Setup]
AppId={{6B1E5E0A-3C8E-4C0B-9E3E-5A1F0B7C2D11}
AppName=Hokma
AppVersion={#AppVersion}
AppVerName=Hokma {#AppVersion}
AppPublisher=SiomTrading USA LLC
AppPublisherURL=https://github.com/SiomTrading/hokma
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultDirName={%USERPROFILE}\Hokma
UsePreviousAppDir=no
AppendDefaultDirName=no
DirExistsWarning=no
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableWelcomePage=no
CreateUninstallRegKey=yes
UninstallFilesDir={#AppFolder}
UninstallDisplayName=Hokma
UninstallDisplayIcon={uninstallexe}
SetupIconFile=..\assets\hokma.ico
WizardStyle=modern
WizardSizePercent=100
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage
CloseApplications=force
RestartApplications=no
SetupLogging=yes
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=Hokma-Setup
#ifdef SignTool
; Signing is prepared and off: the workflow passes /DSignTool and /Shokma=... only when signing is turned on.
SignTool=hokma
SignedUninstaller=yes
#endif

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl,..\messages\setup-es.isl"
Name: "en"; MessagesFile: "compiler:Default.isl,..\messages\setup-en.isl"

[Files]
; Used during the install only (extracted to {tmp} by the code below).
Source: "setup-helper.ps1"; Flags: dontcopy
Source: "{#SourceZip}"; DestName: "hokma-src.zip"; Flags: dontcopy nocompression
#ifdef WebView2Setup
Source: "{#WebView2Setup}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
#endif
; The desktop window.
#ifdef AppExe
Source: "{#AppExe}"; DestDir: "{#AppFolder}"; DestName: "Hokma.exe"; Flags: ignoreversion
#endif
; What the uninstaller needs: uninstall.ps1 with its messages, and the helper to close Hokma first.
Source: "setup-helper.ps1"; DestDir: "{#AppFolder}\uninstall"; Flags: ignoreversion
Source: "..\uninstall.ps1"; DestDir: "{#AppFolder}\uninstall"; Flags: ignoreversion
Source: "..\messages.ps1"; DestDir: "{#AppFolder}\uninstall"; Flags: ignoreversion
Source: "..\messages\*.properties"; DestDir: "{#AppFolder}\uninstall\messages"; Flags: ignoreversion

[Run]
Filename: "{#AppFolder}\Hokma.exe"; Description: "{cm:OpenNow}"; Flags: postinstall nowait skipifsilent; Check: AppUsable
Filename: "{#AppFolder}\Hokma.exe"; Flags: nowait; Check: AppUsable and ReopenWhenSilent
Filename: "{%USERPROFILE}\.local\bin\hokma.cmd"; Parameters: "start"; Description: "{cm:OpenNow}"; Flags: postinstall nowait runhidden skipifsilent; Check: not AppUsable
Filename: "{%USERPROFILE}\.local\bin\hokma.cmd"; Parameters: "start"; Flags: nowait runhidden; Check: (not AppUsable) and ReopenWhenSilent

[UninstallRun]
; Close Hokma, then remove the engine, the command and the shortcuts. Never -Purge: the working folder and the
; Claude session stay (CA13).
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{#AppFolder}\uninstall\setup-helper.ps1"" -Action Stop -AppDir ""{#AppFolder}"""; Flags: runhidden waituntilterminated; RunOnceId: "HokmaStop"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{#AppFolder}\uninstall\uninstall.ps1"""; Flags: runhidden waituntilterminated; RunOnceId: "HokmaUninstall"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Hokma-App"
Type: filesandordirs; Name: "{#AppFolder}\uninstall"

[Code]
var
  Detected, HasGit, HasClaude: Boolean;
  PrevWorkdir, PrevVersion: String;
  ModePage, ComponentsPage: TInputOptionWizardPage;
  ProgressPage: TOutputProgressPage;
  LogFile, BackupPath, KeptFailedPath, HelperError: String;
  RunDone, ClaudeFailed, RestoreFailed, WebView2Missing: Boolean;
  StageCount: Integer;

const
  StageTotal = 9;

function PowerShellExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function HelperPath: String;
begin
  Result := ExpandConstant('{tmp}\setup-helper.ps1');
end;

function AppFolder: String;
begin
  Result := ExpandConstant('{#AppFolder}');
end;

function LangCode: String;
begin
  if ActiveLanguage = 'en' then Result := 'en' else Result := 'es';
end;

function HelperParams(const Action: String): String;
begin
  Result := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + HelperPath + '" -Action ' + Action +
            ' -Log "' + LogFile + '" -AppDir "' + AppFolder + '"';
end;

function ParamMode: String;
begin
  Result := Lowercase(ExpandConstant('{param:MODE|}'));
  if (Result <> 'fresh') and (Result <> 'update') and (Result <> 'reinstall') then Result := '';
end;

function WaitPid: Integer;
begin
  Result := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
end;

function SelectedMode: String;
begin
  Result := ParamMode;
  if Result <> '' then exit;
  if not Detected then Result := 'fresh'
  else if ModePage.SelectedValueIndex = 0 then Result := 'update'
  else Result := 'reinstall';
end;

function WantClaude: Boolean;
begin
  Result := (not HasClaude) and ComponentsPage.Values[0] and (Pos('/NOCLAUDE', Uppercase(GetCmdTail)) = 0);
end;

{ A path inside any OneDrive folder of this user. }
function InOneDrive(const Path: String): Boolean;
var
  Names: array of String;
  I: Integer;
  Root, Target: String;
begin
  Result := False;
  SetArrayLength(Names, 3);
  Names[0] := 'OneDrive'; Names[1] := 'OneDriveConsumer'; Names[2] := 'OneDriveCommercial';
  Target := Lowercase(AddBackslash(ExpandFileName(Path)));
  for I := 0 to 2 do begin
    Root := GetEnv(Names[I]);
    if Root <> '' then begin
      Root := Lowercase(AddBackslash(ExpandFileName(Root)));
      if Copy(Target, 1, Length(Root)) = Root then begin
        Result := True;
        exit;
      end;
    end;
  end;
end;

function AppUsable: Boolean;
begin
  Result := FileExists(AppFolder + '\Hokma.exe') and not WebView2Missing;
end;

function ReopenWhenSilent: Boolean;
begin
  Result := WizardSilent and (WaitPid > 0);
end;

function BackupPreview: String;
begin
  Result := RemoveBackslashUnlessRoot(WizardDirValue) + '-' + CustomMessage('BackupWord') + '-' +
            GetDateTimeString('yyyymmdd-hhnn', #0, #0);
end;

{ ---- detection ---------------------------------------------------------------------------------------------- }

procedure ReadDetection(const OutFile: String);
var
  Lines: TArrayOfString;
  I, P: Integer;
  K, V: String;
begin
  if not LoadStringsFromFile(OutFile, Lines) then exit;
  for I := 0 to GetArrayLength(Lines) - 1 do begin
    P := Pos('=', Lines[I]);
    if P > 0 then begin
      K := Copy(Lines[I], 1, P - 1);
      V := Trim(Copy(Lines[I], P + 1, MaxInt));
      if (Length(K) > 0) and (Ord(K[1]) = $FEFF) then Delete(K, 1, 1);
      if K = 'found' then Detected := V = '1'
      else if K = 'workdir' then PrevWorkdir := V
      else if K = 'version' then PrevVersion := V
      else if K = 'git' then HasGit := V = '1'
      else if K = 'claude' then HasClaude := V = '1';
    end;
  end;
end;

function InitializeSetup: Boolean;
var
  Code: Integer;
  OutFile: String;
begin
  Result := True;
  ForceDirectories(ExpandConstant('{localappdata}\Hokma-Setup'));
  LogFile := ExpandConstant('{localappdata}\Hokma-Setup\setup-') + GetDateTimeString('yyyymmdd-hhnnss', #0, #0) + '.log';
  ExtractTemporaryFile('setup-helper.ps1');
  OutFile := ExpandConstant('{tmp}\detect.txt');
  if Exec(PowerShellExe, HelperParams('Detect') + ' -Out "' + OutFile + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    ReadDetection(OutFile);
  Log(Format('detected=%d workdir=%s version=%s git=%d claude=%d', [Ord(Detected), PrevWorkdir, PrevVersion, Ord(HasGit), Ord(HasClaude)]));
end;

{ ---- wizard pages ------------------------------------------------------------------------------------------- }

procedure InitializeWizard;
var
  VersionText, GitLine: String;
begin
  VersionText := PrevVersion;
  if VersionText = '' then VersionText := CustomMessage('UnknownVersion');
  ModePage := CreateInputOptionPage(wpWelcome, CustomMessage('ModeCaption'),
    FmtMessage(CustomMessage('ModeDescription'), [VersionText, PrevWorkdir]), CustomMessage('ModeSubCaption'), True, False);
  ModePage.Add(CustomMessage('ModeUpdate'));
  ModePage.Add(CustomMessage('ModeReinstall'));
  ModePage.SelectedValueIndex := 1;          { decision D2 (Cristian, 10-nov): Reinstall from scratch by default }
  if ParamMode = 'update' then ModePage.SelectedValueIndex := 0;

  if HasGit then GitLine := CustomMessage('GitPresent') else GitLine := CustomMessage('GitWillInstall');
  ComponentsPage := CreateInputOptionPage(wpSelectDir, CustomMessage('ComponentsCaption'),
    CustomMessage('ComponentsDescription'), GitLine, False, False);
  if HasClaude then begin
    ComponentsPage.Add(CustomMessage('ClaudePresent'));
    ComponentsPage.Values[0] := False;
    ComponentsPage.CheckListBox.ItemEnabled[0] := False;
  end else begin
    ComponentsPage.Add(CustomMessage('InstallClaude'));
    ComponentsPage.Values[0] := True;
  end;

  ProgressPage := CreateOutputProgressPage(SetupMessage(msgWizardPreparing), '');

  if (ExpandConstant('{param:DIR|}') = '') and Detected and (PrevWorkdir <> '') then
    WizardForm.DirEdit.Text := PrevWorkdir;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = ModePage.ID then
    Result := (not Detected) or (ParamMode <> '')
  else if PageID = wpSelectDir then
    { Update keeps the current folder; it cannot be changed. }
    Result := (SelectedMode = 'update') and (PrevWorkdir <> '');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ModePage.ID) and (SelectedMode = 'update') and (PrevWorkdir <> '') then
    WizardForm.DirEdit.Text := PrevWorkdir
  else if (CurPageID = wpSelectDir) and InOneDrive(WizardDirValue) then begin
    MsgBox(CustomMessage('OneDriveRejected'), mbError, MB_OK);
    Result := False;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
var
  Mode: String;
begin
  Mode := SelectedMode;
  Result := FmtMessage(CustomMessage('ReadyFolder'), [WizardDirValue]) + NewLine;
  if Mode = 'update' then Result := Result + CustomMessage('ReadyModeUpdate') + NewLine
  else if Mode = 'reinstall' then begin
    Result := Result + CustomMessage('ReadyModeReinstall') + NewLine;
    if DirExists(WizardDirValue) then
      Result := Result + NewLine + FmtMessage(CustomMessage('ReadyBackup'), [BackupPreview]) + NewLine;
    Result := Result + CustomMessage('ReadyKept') + NewLine;
  end else Result := Result + CustomMessage('ReadyModeFresh') + NewLine;
  if not HasGit then Result := Result + NewLine + CustomMessage('ReadyGit');
  if WantClaude then Result := Result + NewLine + CustomMessage('ReadyClaude');
end;

{ ---- running the helper ------------------------------------------------------------------------------------- }

function StageText(const Name: String): String;
begin
  if Name = 'closing' then Result := CustomMessage('StageClosing')
  else if Name = 'backup' then Result := CustomMessage('StageBackup')
  else if Name = 'reset' then Result := CustomMessage('StageReset')
  else if Name = 'git' then Result := CustomMessage('StageGit')
  else if Name = 'claude' then Result := CustomMessage('StageClaude')
  else if Name = 'install' then Result := CustomMessage('StageInstall')
  else if Name = 'restore' then Result := CustomMessage('StageRestore')
  else if Name = 'shortcuts' then Result := CustomMessage('StageShortcuts')
  else if Name = 'webview' then Result := CustomMessage('StageWebview')
  else Result := CustomMessage('StagePreparing');
end;

procedure ReadResult(const Pair: String);
var
  P: Integer;
  K, V: String;
begin
  P := Pos('=', Pair);
  if P = 0 then exit;
  K := Copy(Pair, 1, P - 1);
  V := Copy(Pair, P + 1, MaxInt);
  if K = 'done' then RunDone := True
  else if K = 'backup' then BackupPath := V
  else if (K = 'claude') and (V = 'failed') then ClaudeFailed := True
  else if (K = 'restore') and (V = 'failed') then RestoreFailed := True
  else if K = 'kept_failed' then KeptFailedPath := V
  else if (K = 'webview2') and ((V = 'failed') or (V = 'missing')) then WebView2Missing := True;
end;

procedure ShowStage(const Text, Detail: String);
begin
  if WizardSilent then exit;
  if WizardForm.CurPageID = wpInstalling then begin
    WizardForm.StatusLabel.Caption := Text;
    WizardForm.FilenameLabel.Caption := Detail;
  end else
    ProgressPage.SetText(Text, Detail);
end;

procedure OnHelperLine(const S: String; const Error, FirstLine: Boolean);
begin
  if Copy(S, 1, 6) = 'STAGE:' then begin
    StageCount := StageCount + 1;
    ShowStage(StageText(Copy(S, 7, MaxInt)), '');
    if (not WizardSilent) and (WizardForm.CurPageID <> wpInstalling) then ProgressPage.SetProgress(StageCount, StageTotal);
  end
  else if Copy(S, 1, 5) = 'LINE:' then begin
    if (not WizardSilent) and (WizardForm.CurPageID <> wpInstalling) then
      ProgressPage.SetText(ProgressPage.Msg1Label.Caption, Copy(S, 6, MaxInt));
  end
  else if Copy(S, 1, 7) = 'RESULT:' then ReadResult(Copy(S, 8, MaxInt))
  else if Copy(S, 1, 6) = 'ERROR:' then HelperError := Copy(S, 7, MaxInt);
  Log(S);
end;

{ Every stage before files are copied: close Hokma, back up, reset, Git, Claude Code, install.ps1, restore. A failure
  stops the setup here, with nothing half replaced and the log path on screen (CA10). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Params, Zip: String;
  Ok: Boolean;
begin
  Result := '';
  if InOneDrive(WizardDirValue) then begin
    Result := CustomMessage('OneDriveRejected');
    exit;
  end;
  ExtractTemporaryFile('hokma-src.zip');
  Zip := ExpandConstant('{tmp}\hokma-src.zip');
  Params := HelperParams('Run') + ' -Mode ' + SelectedMode + ' -Dir "' + RemoveBackslashUnlessRoot(WizardDirValue) +
            '" -Src "' + Zip + '" -Lang ' + LangCode + ' -BackupWord "' + CustomMessage('BackupWord') + '"';
  if WaitPid > 0 then Params := Params + ' -WaitFor ' + IntToStr(WaitPid);
  if WantClaude then Params := Params + ' -InstallClaude';
  Log('helper: ' + Params);
  StageCount := 0;
  if not WizardSilent then begin
    ProgressPage.SetText(CustomMessage('StagePreparing'), '');
    ProgressPage.SetProgress(0, StageTotal);
    ProgressPage.Show;
  end;
  try
    Ok := ExecAndLogOutput(PowerShellExe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnHelperLine);
  finally
    if not WizardSilent then ProgressPage.Hide;
  end;
  if (not Ok) or (Code <> 0) or (not RunDone) then begin
    Log(Format('helper failed: ok=%d code=%d error=%s', [Ord(Ok), Code, HelperError]));
    Result := FmtMessage(CustomMessage('Failed'), [LogFile]);
  end;
end;

{ After Hokma.exe is in place: the shortcut opens the window, and WebView2 is installed when missing. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Params: String;
begin
  if CurStep <> ssPostInstall then exit;
  Params := HelperParams('Finish');
#ifdef WebView2Setup
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  Params := Params + ' -WebView2 "' + ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe') + '"';
#endif
  if not FileExists(AppFolder + '\Hokma.exe') then Params := Params + ' -NoApp';
  if not ExecAndLogOutput(PowerShellExe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnHelperLine) then
    Log('finish step could not run');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Notes: String;
begin
  if CurPageID <> wpFinished then exit;
  Notes := '';
  if ClaudeFailed or ((not HasClaude) and (not WantClaude)) then Notes := Notes + #13#10#13#10 + CustomMessage('ClaudeLater');
  if RestoreFailed and (BackupPath <> '') then Notes := Notes + #13#10#13#10 + FmtMessage(CustomMessage('RestoreFailed'), [BackupPath]);
  if KeptFailedPath <> '' then Notes := Notes + #13#10#13#10 + FmtMessage(CustomMessage('KeptFailed'), [KeptFailedPath]);
  if WebView2Missing and FileExists(AppFolder + '\Hokma.exe') then Notes := Notes + #13#10#13#10 + CustomMessage('WebView2Missing');
  if Notes <> '' then WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + Notes;
end;
