# mm3-build

Hatchling build hook `mm3-i18n` for MM3 packages: it compiles every
`locales/<lang>/LC_MESSAGES/messages.po` of the package into a `messages.mo`
and a `<lang>/messages.json` inside the wheel, and fails the build when a
catalog cannot be compiled (core spec CORE-263, CORE-280). It depends only on
hatchling and Babel and is versioned with the MM3 Module API.

```toml
[build-system]
requires = ["hatchling", "mm3-build>=1.0,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.hooks.mm3-i18n]
```

`mm3-core` builds its own catalogs with it, and `mm3 i18n compile` and
`mm3 i18n check` use the same compiler (`mm3_build.catalogs`).
