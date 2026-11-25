# realtest/

Real `.fwsc` firmware packages used by two parser tests in `tests/test_web_hub.py`
(`test_real_packages`, `test_real_packages_report_regions_and_key`). They are not in the
repository: they are other people's firmware, and most do not state redistribution terms.

Without them those two tests are **skipped** and everything else runs.

To fetch them (checksums come from The Firmware Hub's catalogue at run time):

    sh tools/fetch-realtest.sh

Expected files: `sloop-2.8.5.fwsc`, `felucca-1.8.2.fwsc`, `obervave-3.5.2.fwsc`,
`melodee-1.6.1.fwsc`, `choralroot-0.42.fwsc`, `V15-FM-1.fwsc` (fetched), and `SHRUB.fwsc`
(not on the hub; supply your own copy). Both tests skip unless all seven are present.
