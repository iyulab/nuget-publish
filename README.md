# nuget-publish

The single place that pushes NuGet packages to nuget.org for this organization, using
[Trusted Publishing](https://learn.microsoft.com/nuget/nuget-org/trusted-publishing) (OIDC, no long-lived API key).

A package repository builds and packs, uploads the `.nupkg` files as an artifact, and asks this repository
to publish them. Only this repository's `publish.yml` holds a trust policy on nuget.org.

## Why the push runs here

nuget.org matches a policy against both the `repository` and the `job_workflow_ref` claims of the OIDC token.
With a reusable workflow those two point to different repositories (caller and callee), so no policy can match.
The push must therefore run in a workflow of the policy's own repository.

## How it works

```
package repo                                     nuget-publish
  pack -> upload artifact `nuget-packages`
  request.yml (reusable) --workflow_dispatch-->  publish.yml
                                                   plan      allowlist check (PUBLISHERS)
                                                   publish   source run check, download artifact,
                                                             package check, NuGet/login, dotnet nuget push
```

`publish.yml` refuses to push unless all of these hold:

- the source repository is in the `PUBLISHERS` allowlist, and the run came from the workflow the allowlist names;
- the run completed successfully, was not triggered by a pull request, and built code from that repository itself;
- every file in the artifact is a `.nupkg` or `.snupkg` whose package id matches the allowlist entry.

The allowlist matters because the nuget.org policies allow every package of their owner (`*`).

## nuget.org policies

Both policies: repository `nuget-publish`, workflow `publish.yml`, scope *push new packages and versions* (no unlist).

| Policy | Package owner | Environment |
|---|---|---|
| `central-publish-rnd` | `iyulab-rnd` | `release` |
| `central-publish-iyulab` | `iyulab` | `release-iyulab` |

nuget.org hands a token to the oldest matching policy and one API key covers one owner, so the two policies must
differ. The environment tells them apart; `publish.yml` picks it from the allowlist entry's `owner`.
Both GitHub environments accept the `main` branch only.

## Adding a package repository

1. Add it to the `PUBLISHERS` repository variable (format: [`publishers.example.json`](publishers.example.json)):
   `gh variable set PUBLISHERS --repo iyulab/nuget-publish < publishers.json`. The list is a variable, not a
   committed file; note that the names of the repositories that publish still show up in run titles of this
   public repository.
2. Add the repository to the release bot's GitHub App installation and to the selected repositories of the
   organization secret `RELEASE_BOT_PRIVATE_KEY` and variable `RELEASE_BOT_CLIENT_ID`.
3. In the repository's release workflow, upload the packages (artifact `nuget-packages`, `retention-days: 1`,
   only `.nupkg`/`.snupkg`) and call [`request.yml`](.github/workflows/request.yml):

   ```yaml
   publish:
     needs: pack
     uses: iyulab/nuget-publish/.github/workflows/request.yml@<sha> # <tag>
     permissions: {}
     secrets: inherit
   ```

Organization secret `NUGET_USER` must be the nuget.org user that created the policies.
