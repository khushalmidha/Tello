param(
    [ValidateSet('setup', 'grade', 'sheet', 'eval', 'validate', 'test')]
    [string]$Task = 'test',
    [string]$Python = '.venv/Scripts/python.exe',
    [int]$Timeout = 600
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
if ($Task -eq 'setup') {
    uv venv --allow-existing --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv pip install --python $Python -r harness/requirements.txt
    exit $LASTEXITCODE
}
switch ($Task) {
    'grade' {
        & $Python grader/run_grader.py --harness 'python harness/harness.py' --data data/scenarios --key data/scenarios/answers --mode both --timeout $Timeout --reports-dir out/practice_reports --out report_card.json |
            Tee-Object -FilePath report_card.txt
    }
    'sheet' { & $Python tools/run_mailbox.py --data data/exam --harness harness/harness.py --out out/exam_report.json --sheet answer_sheet.json --timeout $Timeout }
    'eval' { & $Python evals/run_evals.py --harness harness/harness.py --timeout $Timeout }
    'validate' { & $Python tools/check_submission.py --harness harness/harness.py }
    'test' { & $Python -m unittest discover -s tests -v }
}
exit $LASTEXITCODE
