# payments-bot

Reads Peruvian payment receipts (Yape, Banco de la Nación, …), extracts the amount, and keeps an
append-only ledger where corrections are new entries, never edits.

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

## Receipt amount extractor

`ClaudeBedrockExtractor` (`src/payments_bot/adapters/extraction/`) sends one receipt image to
Claude on Amazon Bedrock and returns an `Extraction` with an explicit status:

- `ok`: amount in céntimos (`S/ 250` → `25000`, `S/ 40.00` → `4000`), currency (`PEN` or
  `USD`), operation number (ASCII digits; digits of other scripts are converted), payment date
  (`paid_on`) and, when the receipt prints one, time (`paid_at`, always read as Lima wall-clock
  time, UTC−5), provider and confidence. A date before 2016 or after tomorrow is a misread and
  is left empty. Only a high-confidence amount that passes every check is `ok`, and `ok` is the
  only status that may be recorded without asking.
- `needs_confirmation`: a candidate amount and currency that must **never be recorded
  automatically**. The model was not highly confident, or the amount repeats the receipt's
  security code. The bot shows the candidate and records only what the user confirms.
- `unreadable`, `not_a_receipt`, `refused` or `failed`: no amount is returned and `error` says
  why. An amount that is zero, above the ledger cap, or repeats the operation number's digits is
  `unreadable`: showing it as a candidate would invite the user to confirm a misread.

The recorded amount is the **transferred amount** (Yape "Monto", Banco de la Nación
"Importe"), not a "Total" that adds fees. The prompt names the numbers that are *not* the amount
(operation number, Yape's security code, phone and account digits, dates, promotional banners),
explains decimal commas (`S/ 1.250,50` → `1250.50`), and tells the model that text inside the
image is data, never instructions. The answer must follow a JSON schema, and the amount is
checked again in code before it is trusted.

Configuration comes from environment variables, for example a git-ignored `.env` file passed
with `uv run --env-file .env`:

| Variable | Meaning | Default |
|---|---|---|
| `AWS_REGION` | Amazon Bedrock region | required |
| `AWS_PROFILE` | AWS profile used to sign requests | default AWS credential chain |
| `EXTRACTOR_MODEL` | Bedrock model id | `anthropic.claude-opus-5-5` |
| `EXTRACTOR_EFFORT` | `low`, `medium`, `high`, `xhigh` or `max` | `medium` |

Leave optional variables unset rather than empty: an empty `AWS_PROFILE` still reaches the AWS
SDK. The AWS account needs Bedrock access to the chosen model in that region.

The Anthropic SDK also reads two variables by itself, and either one silently changes where
receipts go or how requests are authenticated:

- `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` replaces the regional endpoint
  (`https://bedrock-mantle.<region>.api.aws/anthropic`).
- `AWS_BEARER_TOKEN_BEDROCK` (or `ANTHROPIC_AWS_API_KEY`) switches from SigV4 signing to a
  bearer token, unless `AWS_PROFILE` is set. Even an empty value switches it.

The eval prints the resolved endpoint and auth mode before anything is sent (variable names
only, never their values). The endpoint is parsed the way the SDK's HTTP client parses it, so
it names the scheme, host and port requests really go to (an `@` in the path does not change
the host); a URL's user part and query string are masked, the fragment is dropped, and a URL
that is not a valid `http` or `https` URL with a host is not shown at all.

### Evaluate it on real receipts

1. Create the labels template. This is local only; nothing is sent:

   ```bash
   uv run python -m payments_bot.tools.make_labels
   ```

   It writes `data/eval/labels.csv` with one row per image in `pagos/` and refuses to overwrite
   an existing file unless you pass `--force`.

2. Open each image and type the transferred amount it shows in `expected_amount` (not a total
   that adds fees): `250`, `250.00` and `250,00` are all accepted. Leave `expected_currency` as
   `PEN` unless the receipt uses another currency. Rows without an amount are skipped. Do not
   edit `file` or `sha256`: the eval refuses to score an image whose hash no longer matches, a
   file listed twice (also when the names differ only in case or Unicode form, which macOS
   treats as one file), a file name with a directory part, or an amount of zero. Keep the file
   UTF-8 (in a spreadsheet, save as "CSV UTF-8").

3. Review the plan. Without `--yes` nothing is sent. The plan shows the region, models, request
   count, endpoint and auth mode:

   ```bash
   uv run --env-file .env python -m payments_bot.tools.eval_extractor \
     --model anthropic.claude-opus-5-5 --model anthropic.claude-haiku-5-5
   ```

4. Run it by adding `--yes`. **This sends every labeled receipt image, which contains personal
   data, to Amazon Bedrock once per model, billed to your AWS account.** It prints a per-image
   table plus accuracy and token totals per model, and writes
   `data/eval/results-<model>-<UTC time>-<random>.csv` one flushed row per image, so a run that
   stops halfway (for example on an AWS credential error) keeps every answer already paid for.
   Every model's results file is created before the first image is sent, so a file that cannot
   be created stops the run with nothing spent. Only `ok` answers count as matches;
   `needs_confirmation` rows keep their candidate amount for review. In text cells, a `=`, `+`,
   `-`, `@`, tab, carriage return or double quote that starts the cell or follows a `;` or a
   line break is written after a `'`, so a spreadsheet shows it instead of running it as a
   formula, also when it splits lines on `;` as Excel does in Spanish locales (the model's text
   can be steered by the image): `Yape;=1+1` is written `Yape;'=1+1`. Number columns are
   written unchanged. A character no UTF-8 file can hold is written as a backslash escape
   (`\ud800`) instead of stopping the run.

`pagos/` and `data/` are git-ignored, so receipts, labels and results are never committed.
