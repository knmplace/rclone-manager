param(
  [string]$Base = 'B:\Claude_Apps\RCLONE-MANAGER',
  [string]$Version = 'v1.0.1',
  [string]$ZipPath = ''
)

if ([string]::IsNullOrWhiteSpace($ZipPath)) {
  $ZipPath = Join-Path $Base "RCLONE-MANAGER-$Version.zip"
}

if (Test-Path -LiteralPath $ZipPath) {
  Remove-Item -LiteralPath $ZipPath -Force
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$files = Get-ChildItem -LiteralPath $Base -Recurse -File | Where-Object {
  $_.FullName -notmatch '\\.git(\\|$)' -and $_.Name -notmatch '^RCLONE-MANAGER-v.*\.zip$'
}
$zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
  foreach ($file in $files) {
    $entryName = $file.FullName.Substring($Base.Length + 1).Replace('\\', '/')
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
      $zip,
      $file.FullName,
      $entryName,
      [System.IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null
  }
}
finally {
  $zip.Dispose()
}
Write-Output $ZipPath
