# Windows launcher for the map tools -- the Windows counterpart of the Mac
# run_classifier_map.command / run_grid_detector.command files. Start it via
# run_classifier_map.bat or run_grid_detector.bat (double-click), which pass -Tool.
#
# Windows has neither Python nor bash, so this uses only what ships with
# Windows (PowerShell 5.1 and .NET):
#   - First run: downloads uv (a single-file Python/package manager; the exact
#     version is pinned below and checked against a SHA-256 hash before it is
#     ever run) into windows_tools\, then uses it to fetch a private copy of
#     Python and the packages this tool needs into windows_tools\ and venv\.
#     Nothing is installed system-wide and no admin rights are needed; deleting
#     those two folders removes everything.
#   - Every run after that: no network requests from this launcher at all.
#     (The CLIP model update check inside the Python script is separate, and
#     throttled to once per 24 hours there.)
#
# Keep this file pure ASCII: Windows PowerShell 5.1 reads files without a BOM
# as ANSI, so a stray smart quote or emoji can break parsing.

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('classify', 'grid')]
    [string]$Tool,

    # Do the first-run setup (uv, Python, packages) and stop: no dialogs, nothing
    # is run. For preparing a machine ahead of time -- e.g. before going offline --
    # and for CI, which has nobody to click the dialogs.
    [switch]$SetupOnly
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# --- What each launcher runs -------------------------------------------------
# If the .py files are renamed, change Script here (and nowhere else).
# osxphotos (Apple Photos support) is deliberately absent: it is Mac-only.
$ToolConfig = @{
    classify = @{
        Title    = 'RPG Map Categorizer'
        Script   = 'fgu_classify_maps.py'
        Packages = @('torch', 'transformers', 'pillow', 'pillow-heif')
    }
    grid     = @{
        Title    = 'RPG Map Grid Detector'
        Script   = 'fgu_detect_grid.py'
        Packages = @('pillow', 'pillow-heif', 'opencv-python', 'numpy')
    }
}
$Cfg = $ToolConfig[$Tool]

# --- Pinned uv build -----------------------------------------------------------
# To update: pick a release at https://github.com/astral-sh/uv/releases and copy
# the SHA-256 of uv-x86_64-pc-windows-msvc.zip from that release's asset list.
# (The x64 build also runs on Windows-on-ARM through emulation.)
$UvVersion     = '0.13.6'
$UvSha256      = 'a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f'
$PythonVersion = '3.12'

$ToolsDir   = Join-Path $PSScriptRoot 'windows_tools'
$UvExe      = Join-Path $ToolsDir 'uv.exe'
$VenvDir    = Join-Path $PSScriptRoot 'venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

# Keep uv's downloads (Python itself, package cache) inside windows_tools\ too,
# so removing this folder really does remove everything.
$env:UV_CACHE_DIR          = Join-Path $ToolsDir 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ToolsDir 'python'
# Python's output has emoji in it; make sure that can never crash on a
# legacy-codepage console, and stop huggingface_hub nagging about symlinks.
$env:PYTHONUTF8                      = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# --- Dialog helpers (the Windows stand-ins for the Mac osascript dialogs) -------

# A small modal window: a message, optionally a text box, and a row of buttons.
# Returns an object with .Button (the label clicked; $null if the window was
# closed with the X) and .Text (the text box contents, '' if there is none).
function Show-Dialog {
    param(
        [string]$Title,
        [string]$Message,
        [string[]]$Buttons,
        [string]$DefaultButton = '',
        [switch]$AskText,
        [string]$TextDefault = '',
        [int]$MessageHeight = 60
    )
    $width = 560
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MinimizeBox = $false
    $form.MaximizeBox = $false
    $form.TopMost = $true

    # A read-only multi-line text box rather than a Label: it scrolls if the text
    # is longer than expected, so nothing can be cut off. Windows text boxes only
    # understand CR+LF line breaks, hence the replace.
    $body = New-Object System.Windows.Forms.TextBox
    $body.Multiline = $true
    $body.ReadOnly = $true
    $body.TabStop = $false
    $body.BorderStyle = 'None'
    $body.ScrollBars = 'Vertical'
    $body.Text = ($Message -replace "`r?`n", "`r`n")
    $body.SetBounds(14, 12, ($width - 28), $MessageHeight)
    $form.Controls.Add($body)
    $y = 12 + $MessageHeight + 8

    $textBox = $null
    if ($AskText) {
        $textBox = New-Object System.Windows.Forms.TextBox
        $textBox.Text = $TextDefault
        $textBox.SetBounds(14, $y, ($width - 28), 24)
        $form.Controls.Add($textBox)
        $y += 36
    }

    $buttonWidth = 112
    $gap = 8
    $x = $width - 14 - ($Buttons.Count * $buttonWidth) - (($Buttons.Count - 1) * $gap)
    foreach ($label in $Buttons) {
        $button = New-Object System.Windows.Forms.Button
        $button.Text = $label
        $button.SetBounds($x, $y, $buttonWidth, 28)
        # Uses $this (the clicked button), not $label, so it is right for every button.
        $button.Add_Click({
            $parent = $this.FindForm()
            $parent.Tag = $this.Text
            $parent.Close()
        })
        $form.Controls.Add($button)
        if ($label -eq $DefaultButton) { $form.AcceptButton = $button }
        if ($label -eq 'Cancel') { $form.CancelButton = $button }
        $x += $buttonWidth + $gap
    }
    $y += 42
    $form.ClientSize = New-Object System.Drawing.Size($width, $y)

    $form.Add_Shown({ $this.Activate() })
    [void]$form.ShowDialog()
    $result = [pscustomobject]@{
        Button = $form.Tag
        Text   = $(if ($textBox) { $textBox.Text } else { '' })
    }
    $form.Dispose()
    return $result
}

# Folder picker. Returns the chosen path, or $null if cancelled. The invisible
# top-most owner window stops the dialog opening behind other windows.
function Select-Folder([string]$Description) {
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $Description
    $dialog.ShowNewFolderButton = $false
    $answer = $dialog.ShowDialog($owner)
    $owner.Dispose()
    if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { return $dialog.SelectedPath }
    return $null
}

# Tidies a path typed or pasted into a dialog: Explorer's "Copy as path" wraps it
# in quotes, and a trailing backslash on a path containing spaces gets mangled
# when passed to a program (it escapes the closing quote). Drive roots like C:\
# keep their backslash, since C: alone means something else.
function Clean-Path([string]$Path) {
    $Path = $Path.Trim().Trim('"')
    if ($Path.Length -gt 3) { $Path = $Path.TrimEnd('\', '/') }
    return $Path
}

# Asks for one optional numeric setting. Returns @('--flag', 'value') to append
# to the arguments, or an empty array if left blank. Cancel quits.
function Ask-Option([string]$Flag, [string]$Prompt, [string]$Default) {
    $r = Show-Dialog -Title $Cfg.Title -Message $Prompt -AskText -TextDefault $Default `
        -Buttons 'Cancel', 'OK' -DefaultButton 'OK' -MessageHeight 50
    if ($r.Button -ne 'OK') { Write-Host 'Canceled.'; exit 0 }
    $value = $r.Text.Trim()
    if ($value) { return @($Flag, $value) }
    return @()
}

# --- First-run setup ---------------------------------------------------------------

function Ensure-Environment([string]$StampName, [string[]]$Packages) {
    if (-not (Test-Path -LiteralPath $UvExe)) {
        Write-Host "First-time setup: downloading uv $UvVersion (about 15 MB, needs internet)..."
        New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
        $zip = Join-Path $ToolsDir 'uv-download.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'   # the progress bar makes downloads far slower in 5.1
        $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash
        if ($actual -ne $UvSha256) {
            Remove-Item -LiteralPath $zip -Force
            throw "The downloaded uv did not match its expected SHA-256 hash, so it was deleted and not used. Expected $UvSha256 but got $actual."
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $ToolsDir -Force
        Remove-Item -LiteralPath $zip -Force
        if (-not (Test-Path -LiteralPath $UvExe)) { throw 'uv.exe was not found after unpacking the download.' }
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if (Test-Path -LiteralPath $VenvDir) {
            throw "A 'venv' folder exists here but has no Windows Python in it (it may have been made on a Mac, or an earlier setup was interrupted). Delete the 'venv' folder and run this again."
        }
        Write-Host "First-time setup: creating a private Python $PythonVersion environment (downloads Python, needs internet)..."
        & $UvExe venv --python $PythonVersion $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment (exit code $LASTEXITCODE). First-time setup needs an internet connection." }
    }

    # A small stamp file records which package set is installed, so once it is
    # done this makes no network request at all. One stamp per tool, because the
    # two tools install different packages into the same venv.
    $stamp = Join-Path $VenvDir ".deps-$StampName"
    $wanted = "python $PythonVersion`n" + ($Packages -join "`n")
    $have = ''
    if (Test-Path -LiteralPath $stamp) { $have = Get-Content -LiteralPath $stamp -Raw }
    if ($have -ne $wanted) {
        Write-Host "First-time setup: installing $($Packages -join ', ') (several hundred MB; can take a few minutes; needs internet)..."
        & $UvExe pip install --python $VenvPython @Packages
        if ($LASTEXITCODE -ne 0) {
            throw "Installing the packages failed (exit code $LASTEXITCODE). If it mentions a path being too long, move this whole folder somewhere with a shorter path, such as C:\fgu, and try again."
        }
        Set-Content -LiteralPath $stamp -Value $wanted -NoNewline
    }
}

# --- The text shown before anything runs ----------------------------------------------

$ClassifyIntro = @'
This tool uses AI image classification to sort your battle maps into category folders (dungeon, ruin, tavern, and so on), so they are easier to browse and manage.

This does NOT put maps into Fantasy Grounds. Once you have picked out the maps you actually want to use, you still need to manually copy them (and their grid .xml file, if you have run the grid detector) into your Fantasy Grounds campaign images folder.

See the README for full details.
'@

$GridIntro = @'
This tool detects each map battle-map grid and writes a Fantasy Grounds sidecar .xml file next to it, so the grid lines up automatically when imported.

This does NOT put maps into Fantasy Grounds. You still need to manually copy both the map image and its .xml file into your campaign images folder (same folder, not a subfolder).

Then in Fantasy Grounds: in the sidebar go to Library, click Assets, click Images, then click "Refresh Folder Assets" (bottom right). Click the image, then click "Create Image Record". In the opened map, click Grid, then click the eye icon to make the grid visible.

Tip: the Images panel also has a link (bottom right) that opens the images folder in File Explorer, handy for copying files over.

See the README for full details.
'@

# --- Main flow -----------------------------------------------------------------------

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'This needs 64-bit Windows.' }

    $scriptPath = Join-Path $PSScriptRoot $Cfg.Script
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "Cannot find $($Cfg.Script) next to this launcher. If it was renamed, update the Script name near the top of run_windows.ps1."
    }

    if ($SetupOnly) {
        Ensure-Environment $Tool $Cfg.Packages
        Write-Host 'Setup complete.'
        exit 0
    }

    # 1. Explain what this does before touching anything, with a way to read more.
    if ($Tool -eq 'classify') { $introText = $ClassifyIntro; $introHeight = 200 }
    else                      { $introText = $GridIntro;     $introHeight = 280 }
    while ($true) {
        $r = Show-Dialog -Title $Cfg.Title -Message $introText -Buttons 'Cancel', 'Open README', 'Continue' `
            -DefaultButton 'Continue' -MessageHeight $introHeight
        if ($r.Button -eq 'Open README') {
            # notepad rather than the default .md handler, which may not exist.
            Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + (Join-Path $PSScriptRoot 'README.md') + '"')
            continue
        }
        if ($r.Button -eq 'Continue') { break }
        Write-Host 'Canceled.'
        exit 0
    }

    # 2. Which folder of maps. (No Apple Photos albums on Windows.)
    $folder = Select-Folder 'Select your Battle Maps folder:'
    if (-not $folder) { Write-Host 'Canceled.'; exit 0 }
    $PyArgs = @('--dir', (Clean-Path $folder))

    # 3. Tool-specific choices.
    if ($Tool -eq 'classify') {
        # Only asks WHERE; the Python script itself warns how many files are
        # affected and asks for confirmation before moving anything.
        $r = Show-Dialog -Title $Cfg.Title -AskText -TextDefault (Clean-Path $folder) `
            -Message 'Destination directory to sort classified files into (a subfolder per category will be created inside it). Leave it blank to classify only, without moving anything.' `
            -Buttons 'Cancel', 'OK' -DefaultButton 'OK' -MessageHeight 60
        if ($r.Button -ne 'OK') { Write-Host 'Canceled.'; exit 0 }
        $moveTo = Clean-Path $r.Text
        if ($moveTo) { $PyArgs += @('--move-to', $moveTo) } else { $PyArgs += '--no-move' }
    }
    else {
        $r = Show-Dialog -Title $Cfg.Title -Buttons 'Cancel', 'Skip Existing', 'Overwrite' -DefaultButton 'Skip Existing' `
            -Message 'Some maps may already have an FGU grid sidecar (.xml) file. Overwrite existing sidecars, or skip them?' `
            -MessageHeight 50
        if ($r.Button -eq 'Overwrite') { $PyArgs += '--force' }
        elseif ($r.Button -ne 'Skip Existing') { Write-Host 'Canceled.'; exit 0 }
    }

    # 4. Advanced parameters (optional). Blank answers keep the script's default.
    $r = Show-Dialog -Title $Cfg.Title -Buttons 'Cancel', 'Use Defaults', 'Customize' -DefaultButton 'Use Defaults' `
        -Message 'Use default detection settings, or customize advanced parameters?' -MessageHeight 30
    if ($r.Button -eq 'Customize') {
        if ($Tool -eq 'classify') {
            $PyArgs += @(Ask-Option '--unclassified-threshold' 'Minimum CLIP confidence (%) before tagging an image "unclassified" instead of forcing the closest category:' '25')
        }
        else {
            $PyArgs += @(Ask-Option '--grid-min-px' 'Minimum grid square size to consider, in pixels:' '20')
            $PyArgs += @(Ask-Option '--grid-max-fraction' 'Maximum grid square size, as a fraction of the shorter image side:' '0.25')
            $PyArgs += @(Ask-Option '--grid-min-confidence' 'Minimum grid-detection confidence (0-1) before reporting "no grid detected":' '0.15')
            $PyArgs += @(Ask-Option '--grid-min-line-coverage' 'Minimum fraction (0-1) of the image a candidate grid line must actually span to count as real (rejects photo textures like railings or decking):' '0.5')
        }
    }
    elseif ($r.Button -ne 'Use Defaults') { Write-Host 'Canceled.'; exit 0 }

    # 5. Make sure Python and the packages exist (a no-op after the first run).
    Ensure-Environment $Tool $Cfg.Packages

    # 6. Run the script. Its own prompts (move confirmation, model download) are
    #    answered right here in this console window.
    Write-Host ''
    Write-Host "Starting $($Cfg.Title)..."
    & $VenvPython $scriptPath @PyArgs
    $code = $LASTEXITCODE
    Write-Host ''
    if ($code -eq 0) { Write-Host 'Done.' } else { Write-Host "Finished with errors (exit code $code)." }
    exit $code
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
