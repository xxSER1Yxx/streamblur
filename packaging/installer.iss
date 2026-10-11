; Установщик StreamBlur для Inno Setup 6.
;
; Сборка (Windows, после `pyinstaller packaging/streamblur.spec`):
;
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; Результат: packaging\Output\StreamBlur-Setup-<версия>.exe
;
; В GitHub Actions Inno Setup ставится автоматически — см. .github/workflows/build.yml

#define MyAppName "StreamBlur"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "StreamBlur"

; ┌──────────────────────────────────────────────────────────────────────────┐
; │  Ссылка на репозиторий: она же в streamblur/__init__.py (__repo_url__).   │
; │  Отсюда ссылка попадёт в «Программы и компоненты», в свойства ярлыка,    │
; │  в кнопку «Поддержка» установщика и в окно после установки.              │
; └──────────────────────────────────────────────────────────────────────────┘
#define MyAppURL "https://github.com/xxSER1Yxx/streamblur"
#define MyAppRepoName "streamblur"
#define MyAppExeName "StreamBlur.exe"

[Setup]
AppId={{7C2F1E64-3B0A-4B7E-9C1A-STREAMBLUR01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Ставим БЕЗ прав администратора — стримеру не нужно ничего подтверждать
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=auto
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=StreamBlur-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; Куда писать, если что-то сломалось, и где поддержать проект
ShowLanguageDialog=no
; Одна копия программы: повторный запуск установщика не плодит папки
UsePreviousAppDir=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Всё, что собрал PyInstaller в папку dist\StreamBlur
Source: "..\dist\StreamBlur\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Демо (без OBS)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "demo"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; кэш OCR-движка, если он создавался рядом с программой
Type: filesandordirs; Name: "{app}\_internal\cache"

[Code]
// После установки показываем, куда писать и где поддержать проект.
// Ссылка берётся из #define MyAppURL в начале файла — менять только там.
// Подсказка про OBS: без obs-websocket блюр не переключится.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if MsgBox('StreamBlur управляет размытием через obs-websocket.' + #13 + #10 +
              'В OBS: Инструменты → Настройки obs-websocket → включить сервер.' + #13 + #10 + #13 + #10 +
              'Сообщить о проблеме или поддержать проект:' + #13 + #10 +
              '{#MyAppURL}' + #13 + #10 + #13 + #10 +
              'Открыть страницу проекта сейчас?', mbConfirmation, MB_YESNO) = IDYES then
    begin
      ShellExec('open', '{#MyAppURL}', '', '', SW_SHOW, ewNoWait, ResultCode);
    end;
  end;
end;
