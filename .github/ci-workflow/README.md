# CI workflow

`ci.yml` is the GitHub Actions pipeline: on every push it runs the experiment
smoke tests and builds/serves the container image.

It is parked here (not under `.github/workflows/`) so it can be committed with
a token that lacks the `workflow` OAuth scope. To activate it:

```bash
gh auth refresh -h github.com -s workflow      # grant the scope once
git mv .github/ci-workflow/ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions" && git push
```

GitHub then runs it automatically on the next push.
