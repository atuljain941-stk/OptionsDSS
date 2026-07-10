param(
  [string]$BaseUrl = 'http://127.0.0.1:5050',
  [string]$Out = 'oiapp_regression_report.json',
  [string]$Browser = 'chrome'
)

python .\oiapp_regression_suite.py --base-url $BaseUrl --out $Out --browser $Browser
