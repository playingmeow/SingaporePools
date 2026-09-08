# SingaporePools

This repository automatically updates Singapore Pools 4D and TOTO prize results.

Credits to [foooooooooooooooooooooooooootw](https://github.com/foooooooooooooooooooooooooootw), who maintained the original dataset until Jul 2026, when I chanced upon it on 1 Sep 2026.

Original dataset: [Singapore-Pools-Dataset](https://github.com/foooooooooooooooooooooooooootw/Singapore-Pools-Dataset)

## Updates

The 4D results file is automatically updated every Monday and should be current by Monday noon (Singapore time).

The TOTO results file is automatically updated every Tuesday and Friday and should be current by noon on those days (Singapore time).

## Dataset notes

The 4D dataset contains Singapore Pools 4D prize-winning numbers from 31 May 1986 onward.

TOTO numbers that are single digit are padded with a leading zero.

The inherited TOTO dataset starts from draw 1001. Some early rows contain the year `0001`, and some historical draws contain `NaN`. These were already present in the original dataset and appear to be due to incomplete or erroneous data returned by the Singapore Pools website.

Your CSV viewer may omit leading zeroes.
