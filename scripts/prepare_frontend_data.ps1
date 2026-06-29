param(
    [string]$InputPath = ".\demo_data\movies.csv",
    [string]$OutputPath = ".\frontend\static\data\movies.json"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($InputPath -eq ".\demo_data\movies.csv") {
    $InputPath = Join-Path $ProjectRoot "demo_data\movies.csv"
}
if ($OutputPath -eq ".\frontend\static\data\movies.json") {
    $OutputPath = Join-Path $ProjectRoot "frontend\static\data\movies.json"
}

if (!(Test-Path $InputPath)) {
    throw "Input file not found: $InputPath"
}

$outputDir = Split-Path -Parent $OutputPath
if ($outputDir -and !(Test-Path $outputDir)) {
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
}

function Get-YearFromMovieLensTitle {
    param([string]$Title)
    if ($Title -match '\((\d{4})\)\s*$') {
        return $Matches[1]
    }
    return ""
}

function Get-TitleFromMovieLensTitle {
    param([string]$Title)
    if ($Title -match '^(.*)\s\(\d{4}\)\s*$') {
        return $Matches[1].Trim()
    }
    return $Title
}

$rows = Import-Csv $InputPath
$movies = foreach ($row in $rows) {
    $genres = @()
    if (![string]::IsNullOrWhiteSpace($row.tmdb_genres)) {
        $genres = @($row.tmdb_genres -split '\|' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    } elseif (![string]::IsNullOrWhiteSpace($row.movielens_genres)) {
        $genres = @($row.movielens_genres -split '\|' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_ -ne '(no genres listed)' })
    }

    $title = if (![string]::IsNullOrWhiteSpace($row.tmdb_title)) {
        $row.tmdb_title
    } else {
        Get-TitleFromMovieLensTitle -Title $row.movielens_title
    }

    $year = if (![string]::IsNullOrWhiteSpace($row.release_date) -and $row.release_date.Length -ge 4) {
        $row.release_date.Substring(0, 4)
    } else {
        Get-YearFromMovieLensTitle -Title $row.movielens_title
    }

    [PSCustomObject]@{
        id            = [int]$row.movie_id
        title         = $title
        originalTitle = $row.original_title
        year          = $year
        genres        = $genres
        poster        = $row.poster_url
        overview      = $row.overview
        score         = if ([string]::IsNullOrWhiteSpace($row.vote_average)) { 0 } else { [double]$row.vote_average }
        voteCount     = if ([string]::IsNullOrWhiteSpace($row.vote_count)) { 0 } else { [int]([double]$row.vote_count) }
        popularity    = if ([string]::IsNullOrWhiteSpace($row.popularity)) { 0 } else { [double]$row.popularity }
        releaseDate   = $row.release_date
        runtime       = $row.runtime
        mediaType     = if ([string]::IsNullOrWhiteSpace($row.media_type)) { "movie" } else { $row.media_type }
    }
}

$json = $movies | ConvertTo-Json -Depth 5
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath), $json, $utf8NoBom)

Write-Output "[done] wrote $($movies.Count) rows -> $OutputPath"
