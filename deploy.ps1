<#
.SYNOPSIS
  Copy signal-monitor to the OpenWrt gateway and run install.sh there (needs ssh key access as root).
.EXAMPLE
  .\deploy.ps1                          # root@$env:SIGNALMON_ROUTER, else root@192.168.1.1
  .\deploy.ps1 -Router root@10.0.1.1
#>
param([string]$Router = $(if ($env:SIGNALMON_ROUTER) { $env:SIGNALMON_ROUTER } else { "root@192.168.1.1" }))
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$tar = Join-Path ([IO.Path]::GetTempPath()) "signalmon-src.tar"
$ssh = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=8")

tar -cf $tar -C $src files install.sh uninstall.sh
if ($LASTEXITCODE) { throw "tar failed" }
scp -O @ssh $tar "${Router}:/tmp/signalmon-src.tar"
if ($LASTEXITCODE) { throw "scp failed" }
Remove-Item $tar
ssh @ssh $Router "rm -rf /tmp/signalmon-src && mkdir /tmp/signalmon-src && tar -xf /tmp/signalmon-src.tar -C /tmp/signalmon-src && rm /tmp/signalmon-src.tar && sed -i 's/\r`$//' /tmp/signalmon-src/install.sh && sh /tmp/signalmon-src/install.sh"
if ($LASTEXITCODE) { throw "install.sh failed" }
