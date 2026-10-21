<#
.SYNOPSIS
  Pops a Windows notification when a new internship shows up on the
  SimplifyJobs Summer 2027 list (github.com/SimplifyJobs/Summer2027-Internships).

.EXAMPLE
  .\internship-alert.ps1                 # check once (first run just records what's already listed)
  .\internship-alert.ps1 -SetupPhone     # also send alerts to your phone through the ntfy app
  .\internship-alert.ps1 -Test           # show a sample notification for the newest role
  .\internship-alert.ps1 -LoopMinutes 5  # keep checking every 5 minutes in this window
  .\internship-alert.ps1 -Install        # check every 5 minutes in the background (Task Scheduler)
  .\internship-alert.ps1 -Uninstall      # stop the background checks
#>
param(
    [string]$Category = 'Software',
    [string]$Term = 'Summer 2027',
    [string[]]$Keywords = @(),   # optional, e.g. -Keywords backend,'full stack'
    [int]$LoopMinutes = 0,
    [string]$NtfyTopic = '',     # with -SetupPhone: use this topic instead of a random one
    [string]$NtfyServer = '',    # with -SetupPhone: the server your ntfy app shows (default https://ntfy.sh)
    [switch]$SetupPhone,
    [switch]$Test,
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ListingsUrl = 'https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json'
$StateDir = Join-Path $env:LOCALAPPDATA 'InternshipAlert'
$SeenFile = Join-Path $StateDir 'seen.txt'
$EtagFile = Join-Path $StateDir 'etag.txt'
$LogFile = Join-Path $StateDir 'new-postings.csv'
$NtfyFile = Join-Path $StateDir 'ntfy-topic.txt'
$NtfyServerFile = Join-Path $StateDir 'ntfy-server.txt'
$TaskName = 'Internship Alert'
# Windows PowerShell's own app ID, so notifications work without registering a new app.
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }

function Show-Toast([string]$Title, [string]$Body, [string]$ApplyUrl, [string]$SimplifyUrl) {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
    $actions = "<action content=`"Apply`" activationType=`"protocol`" arguments=`"$(Esc $ApplyUrl)`"/>"
    if ($SimplifyUrl) { $actions += "<action content=`"Simplify`" activationType=`"protocol`" arguments=`"$(Esc $SimplifyUrl)`"/>" }
    $actions += '<action content="Dismiss" activationType="system" arguments="dismiss"/>'
    # scenario="reminder" keeps the notification on screen until you act on it.
    $xml = "<toast scenario=`"reminder`" activationType=`"protocol`" launch=`"$(Esc $ApplyUrl)`">" +
           "<visual><binding template=`"ToastGeneric`"><text>$(Esc $Title)</text><text>$(Esc $Body)</text></binding></visual>" +
           "<audio src=`"ms-winsoundevent:Notification.Reminder`"/><actions>$actions</actions></toast>"
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show(
        [Windows.UI.Notifications.ToastNotification]::new($doc))
}

# Push to the phone through ntfy.sh, if -SetupPhone has been run. A failure here
# only warns, so the desktop notification still goes out.
function Send-Ntfy([string]$Title, [string]$Body, [string]$ApplyUrl, [string]$SimplifyUrl) {
    if (-not (Test-Path $NtfyFile)) { return }
    $actions = @(@{ action = 'view'; label = 'Apply'; url = $ApplyUrl })
    if ($SimplifyUrl) { $actions += @{ action = 'view'; label = 'Simplify'; url = $SimplifyUrl } }
    $payload = @{
        topic = (Get-Content $NtfyFile -Raw).Trim(); title = $Title; message = $Body
        click = $ApplyUrl; actions = $actions; tags = @('briefcase'); priority = 4
    } | ConvertTo-Json -Depth 4
    $server = if (Test-Path $NtfyServerFile) { (Get-Content $NtfyServerFile -Raw).Trim().TrimEnd('/') } else { 'https://ntfy.sh' }
    try {
        Invoke-RestMethod -Method Post -Uri "$server/" -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload)) | Out-Null
    } catch { Write-Warning "Phone alert failed: $_" }
}

function Send-Alert([string]$Title, [string]$Body, [string]$ApplyUrl, [string]$SimplifyUrl) {
    Send-Ntfy $Title $Body $ApplyUrl $SimplifyUrl
    Show-Toast $Title $Body $ApplyUrl $SimplifyUrl
}

function Send-RoleAlert($role) {
    $posted = [DateTimeOffset]::FromUnixTimeSeconds($role.date_posted).ToLocalTime().ToString('MMM d, h:mm tt')
    $body = "$(@($role.locations) -join ' / ')`nAdded $posted"
    if ($role.sponsorship -and $role.sponsorship -ne 'Other') { $body += " - $($role.sponsorship)" }
    Send-Alert "$($role.company_name): $($role.title)" $body $role.url "https://simplify.jobs/p/$($role.id)"
}

# Returns @{ Listings; ETag }, or $null when nothing changed since the last download.
function Get-Listings([switch]$IgnoreCache) {
    $headers = @{}
    if (-not $IgnoreCache -and (Test-Path $EtagFile)) { $headers['If-None-Match'] = (Get-Content $EtagFile -Raw).Trim() }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $ListingsUrl -Headers $headers
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 304) { return $null }
        throw
    }
    $json = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
    @{ Listings = ($json | ConvertFrom-Json); ETag = $r.Headers['ETag'] }
}

function Select-Roles($listings) {
    $roles = $listings | Where-Object { $_.active -and $_.is_visible -and $_.category -eq $Category -and (@($_.terms) -contains $Term) }
    if ($Keywords) {
        $roles = $roles | Where-Object { $t = $_.title; @($Keywords | Where-Object { $t -match [regex]::Escape($_) }).Count -gt 0 }
    }
    @($roles)
}

function Invoke-Check {
    New-Item -ItemType Directory -Force $StateDir | Out-Null
    $result = Get-Listings
    if (-not $result) { return }
    $roles = Select-Roles $result.Listings

    $firstRun = -not (Test-Path $SeenFile)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    if (-not $firstRun) { Get-Content $SeenFile | ForEach-Object { [void]$seen.Add($_) } }
    $new = @($roles | Where-Object { -not $seen.Contains($_.id) } | Sort-Object date_posted)

    if ($firstRun) {
        Send-Alert 'Internship alerts are on' "Tracking $($roles.Count) open $Category roles. You'll be notified when a new one is posted." 'https://github.com/SimplifyJobs/Summer2027-Internships'
    } elseif ($new.Count -gt 0) {
        $new | Select-Object company_name, title, @{n = 'locations'; e = { @($_.locations) -join '; ' } },
            @{n = 'added'; e = { [DateTimeOffset]::FromUnixTimeSeconds($_.date_posted).ToLocalTime().ToString('yyyy-MM-dd HH:mm') } }, url |
            Export-Csv -Path $LogFile -Append -NoTypeInformation -Encoding UTF8
        $new | Select-Object -Last 4 | ForEach-Object { Send-RoleAlert $_ }
        if ($new.Count -gt 4) {
            Send-Alert "$($new.Count) new $Category internships" "Showing the newest 4. Full list in $LogFile" 'https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/README.md#-software-engineering-internship-roles'
        }
        Write-Host "$($new.Count) new role(s):"; $new | ForEach-Object { Write-Host "  $($_.company_name) - $($_.title)  $($_.url)" }
    } else {
        Write-Host "$(Get-Date -Format t) no new roles ($($roles.Count) tracked)"
    }

    # Record state only after notifying, so a failed run gets retried next time.
    if ($new.Count -gt 0) { $new | ForEach-Object { $_.id } | Add-Content -Path $SeenFile }
    elseif ($firstRun) { New-Item -ItemType File $SeenFile | Out-Null }
    if ($result.ETag) { Set-Content -Path $EtagFile -Value $result.ETag }
}

if ($SetupPhone) {
    New-Item -ItemType Directory -Force $StateDir | Out-Null
    if ($NtfyTopic) { $topic = $NtfyTopic }
    elseif (Test-Path $NtfyFile) { $topic = (Get-Content $NtfyFile -Raw).Trim() }
    else {
        # ntfy topics are public to anyone who knows the name, so make it unguessable.
        $chars = 'abcdefghijkmnpqrstuvwxyz23456789'
        $bytes = New-Object byte[] 12
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $topic = 'swe-alerts-' + -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    }
    Set-Content -Path $NtfyFile -Value $topic
    if ($NtfyServer) {
        if ($NtfyServer -notmatch '^https?://') { $NtfyServer = "https://$NtfyServer" }
        Set-Content -Path $NtfyServerFile -Value $NtfyServer
    }
    $server = if (Test-Path $NtfyServerFile) { (Get-Content $NtfyServerFile -Raw).Trim() } else { 'https://ntfy.sh' }
    Write-Host "Phone alerts will go to ntfy topic: $topic on $server"
    Write-Host "In the ntfy iPhone app, tap + and subscribe to that exact name, then run: .\internship-alert.ps1 -Test"
    return
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

if ($Install) {
    $extra = "-Category `"$Category`" -Term `"$Term`""
    if ($Keywords) { $extra += " -Keywords " + (($Keywords | ForEach-Object { "`"$_`"" }) -join ',') }
    # conhost --headless runs it with no console window, so nothing flashes on screen every 5 minutes.
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -WorkingDirectory $PSScriptRoot `
        -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $extra"
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Scheduled '$TaskName' to check every 5 minutes. Remove it with: .\internship-alert.ps1 -Uninstall"
    return
}

if ($Test) {
    $roles = Select-Roles (Get-Listings -IgnoreCache).Listings
    $newest = $roles | Sort-Object date_posted -Descending | Select-Object -First 1
    Send-RoleAlert $newest
    Write-Host "Sample notification: $($newest.company_name) - $($newest.title)"
    return
}

if ($LoopMinutes -gt 0) {
    while ($true) {
        try { Invoke-Check } catch { Write-Warning $_ }
        Start-Sleep -Seconds ($LoopMinutes * 60)
    }
} else {
    Invoke-Check
}
