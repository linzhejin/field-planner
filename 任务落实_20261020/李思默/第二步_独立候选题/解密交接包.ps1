#requires -Version 7.4
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [System.Security.SecureString]$Password
)

$ErrorActionPreference = 'Stop'
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$archiveName = [System.IO.Path]::GetFileName($archivePath)
$manifestPath = Join-Path $PSScriptRoot '归档校验_SHA256.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = @($manifest.archives | Where-Object {
    [System.IO.Path]::GetFileName($_.path) -eq $archiveName
})
if ($entry.Count -ne 1) {
    throw '该文件未登记在随包校验清单中。'
}
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry[0].sha256) {
    throw '加密文件SHA-256不符，请重新下载。'
}
[byte[]]$blob = [System.IO.File]::ReadAllBytes($archivePath)
if ($blob.Length -lt 53 -or [System.Text.Encoding]::ASCII.GetString($blob, 0, 8) -ne 'NSWENC01') {
    throw '不支持的加密归档格式。'
}

[byte[]]$headerBytes = [byte[]]::new(36)
[byte[]]$saltBytes = [byte[]]::new(16)
[byte[]]$nonceBytes = [byte[]]::new(12)
[byte[]]$tagBytes = [byte[]]::new(16)
[byte[]]$cipherBytes = [byte[]]::new($blob.Length - 52)
[byte[]]$plainBytes = [byte[]]::new($cipherBytes.Length)
[System.Buffer]::BlockCopy($blob, 0, $headerBytes, 0, 36)
[System.Buffer]::BlockCopy($blob, 8, $saltBytes, 0, 16)
[System.Buffer]::BlockCopy($blob, 24, $nonceBytes, 0, 12)
[System.Buffer]::BlockCopy($blob, 36, $cipherBytes, 0, $cipherBytes.Length)
[System.Buffer]::BlockCopy($blob, $blob.Length - 16, $tagBytes, 0, 16)

if ($null -eq $Password) {
    $Password = Read-Host '请输入该角色交接包的口令' -AsSecureString
}
$passwordPointer = [IntPtr]::Zero
$cipher = $null
$keyBytes = $null
try {
    $passwordPointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    $plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    [byte[]]$keyBytes = [System.Security.Cryptography.Rfc2898DeriveBytes]::Pbkdf2(
        $plainPassword, $saltBytes, 600000, [System.Security.Cryptography.HashAlgorithmName]::SHA256, 32
    )
    $cipher = [System.Security.Cryptography.AesGcm]::new($keyBytes, 16)
    try {
        $cipher.Decrypt($nonceBytes, $cipherBytes, $tagBytes, $plainBytes, $headerBytes)
    } catch {
        throw '解密认证失败，请检查口令是否对应本包；没有写出任何解密文件。'
    }
    $plainHash = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($plainBytes)).ToLowerInvariant()
    if ($plainHash -ne $entry[0].decrypted_zip_sha256) {
        throw '解密后的归档SHA-256不符，没有写出文件。'
    }
    $resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
    $outputPath = Join-Path $resolvedOutput ([System.IO.Path]::GetFileNameWithoutExtension($archiveName))
    if (Test-Path -LiteralPath $outputPath) {
        throw '目标文件已存在，请选择新的输出目录。'
    }
    [System.IO.Directory]::CreateDirectory($resolvedOutput) | Out-Null
    [System.IO.File]::WriteAllBytes($outputPath, $plainBytes)
    Write-Output ('解密及校验完成：' + $outputPath)
} finally {
    if ($null -ne $cipher) { $cipher.Dispose() }
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    if ($null -ne $keyBytes) { [Array]::Clear($keyBytes, 0, $keyBytes.Length) }
    [Array]::Clear($plainBytes, 0, $plainBytes.Length)
    Remove-Variable plainPassword -ErrorAction SilentlyContinue
}
