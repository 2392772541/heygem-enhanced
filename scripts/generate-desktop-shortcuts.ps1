param(
  [string]$TargetFolder
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$launchersDir = Join-Path $projectRoot "launchers"
$iconsDir = Join-Path $projectRoot "icons"

$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($TargetFolder)) {
  $TargetFolder = Join-Path $desktop "HeyGem-常用启动器"
}

Write-Host "=================================================="
Write-Host "       HeyGem-Enhanced 桌面快捷方式生成工具       "
Write-Host "=================================================="
Write-Host "项目根目录: $projectRoot"
Write-Host "目标目录:   $TargetFolder"
Write-Host ""

$WshShell = New-Object -ComObject WScript.Shell

$shortcuts = @(
  # 01-启动停止
  @{
    SubDir   = "01-启动停止"
    Name     = "HeyGem-启动.lnk"
    Target   = (Join-Path $launchersDir "Start-HeyGem.bat")
    Icon     = (Join-Path $iconsDir "start.ico")
    Desc     = "启动 HeyGem 完整服务 (Docker + 队列调度 + 客户端)"
  },
  @{
    SubDir   = "01-启动停止"
    Name     = "HeyGem-停止.lnk"
    Target   = (Join-Path $launchersDir "Stop-HeyGem.bat")
    Icon     = (Join-Path $iconsDir "stop.ico")
    Desc     = "安全优雅停止 HeyGem 所有相关服务与容器"
  },
  # 02-队列控制
  @{
    SubDir   = "02-队列控制"
    Name     = "查看实时运行状态.lnk"
    Target   = (Join-Path $launchersDir "View-Status.bat")
    Icon     = (Join-Path $iconsDir "index.ico")
    Desc     = "监控进程、Docker、IndexTTS2及任务队列实时状态"
  },
  @{
    SubDir   = "02-队列控制"
    Name     = "暂停任务排队.lnk"
    Target   = (Join-Path $launchersDir "Pause-Queue.bat")
    Icon     = (Join-Path $iconsDir "pause.ico")
    Desc     = "暂停调度器接收新任务 (正在渲染的会完成)"
  },
  @{
    SubDir   = "02-队列控制"
    Name     = "恢复任务排队.lnk"
    Target   = (Join-Path $launchersDir "Resume-Queue.bat")
    Icon     = (Join-Path $iconsDir "resume.ico")
    Desc     = "恢复调度器继续处理排队任务"
  },
  # 03-TTS切换
  @{
    SubDir   = "03-TTS切换"
    Name     = "切换为IndexTTS2.lnk"
    Target   = (Join-Path $launchersDir "Switch-TTS-Index.bat")
    Icon     = (Join-Path $iconsDir "fish.ico")
    Desc     = "切换语音合成为本地高质量 IndexTTS2 模式"
  },
  @{
    SubDir   = "03-TTS切换"
    Name     = "切回原版FishSpeech.lnk"
    Target   = (Join-Path $launchersDir "Switch-TTS-FishSpeech.bat")
    Icon     = (Join-Path $iconsDir "fish.ico")
    Desc     = "切回官方原版 Docker Fish-Speech 模式"
  },
  # 04-维护工具
  @{
    SubDir   = "04-维护工具"
    Name     = "批量导出视频.lnk"
    Target   = (Join-Path $launchersDir "Export-Videos.bat")
    Icon     = (Join-Path $iconsDir "download.ico")
    Desc     = "扫描数据库将已完成视频自动交付复制到成片目录"
  },
  @{
    SubDir   = "04-维护工具"
    Name     = "清理临时缓存.lnk"
    Target   = (Join-Path $launchersDir "Cleanup-Cache.bat")
    Icon     = (Join-Path $iconsDir "delete.ico")
    Desc     = "安全清理孤儿 WAV 与超过 12 小时的渲染目录"
  },
  @{
    SubDir   = "04-维护工具"
    Name     = "批量清理作品.lnk"
    Target   = (Join-Path $launchersDir "Bulk-Delete-Works.bat")
    Icon     = (Join-Path $iconsDir "delete.ico")
    Desc     = "清理旧作品记录与中间成片，安全保留模特底模"
  }
)

foreach ($sc in $shortcuts) {
  $targetDir = Join-Path $TargetFolder $sc.SubDir
  if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
  }

  $lnkPath = Join-Path $targetDir $sc.Name
  $shortcut = $WshShell.CreateShortcut($lnkPath)
  $shortcut.TargetPath = $sc.Target
  $shortcut.WorkingDirectory = $projectRoot
  $shortcut.Description = $sc.Desc
  if (Test-Path $sc.Icon) {
    $shortcut.IconLocation = "$($sc.Icon),0"
  }
  $shortcut.Save()
  Write-Host "[OK] 已生成: $($sc.SubDir)\$($sc.Name)"
}

Write-Host ""
Write-Host "快捷方式生成完成！已放置于: $TargetFolder"
