# PL28 Finite Target Training Closure

## Windows-proven failure
After PL27, the real 4,694,211-row / 3,729-date research panel loaded successfully and training advanced beyond the former DuckDB BinderException. Scikit-learn then rejected the supervised target with:

`Input y contains infinity or a value too large for dtype('float64').`

## Root cause
Feature columns already replaced +/-Infinity with missing values before imputation, but supervised target columns did not. Historical zero/non-finite denominators can therefore produce mathematically undefined `forward_return`; the future equilibrium label can also inherit non-finite values.

## PL28 correction
- Convert only +/-Infinity in supervised target columns to missing.
- Existing `dropna` admission excludes those invalid observations from OOF, WFA and model fitting.
- Preserve every finite target exactly; no clipping/winsorization is introduced.
- Sanitize again after loading an existing feature store so the PL27 store can be reused without rebuilding the 4.69M-row catalogue.
- Add the target sanitation policy to the model specification hash so stale fold/model caches cannot be silently reused.
- Keep scanner, trading, risk, WFA mathematics, factor governance and catalogue materialization byte-frozen.

Broker authority remains `NONE`.
