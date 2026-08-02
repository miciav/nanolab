# fn-init: Java Template Improvements

**Status:** Design specification
**Date:** 2026-07-31
**Implementation status:** Not implemented
**Target repository:** `miciav/nanofaas` (tool at `tools/fn-init`)

## 1. Purpose

This document describes improvements to the `fn-init` Java function scaffolding tool, based on experience implementing the `figlet` function.

The current template produces a minimal, compilable skeleton. However, every new Java function requires manual additions to reach parity with existing functions (`word-stats`, `json-transform`, `roman-numeral`). These additions are mechanical and should be generated.

## 2. Current template gaps

When scaffolding a new Java function, the developer must manually:

| # | Manual step | Lines | Risk |
|---|-------------|-------|------|
| 1 | Add `org.graalvm.buildtools.native` plugin | 1 | Native image builds silently disabled |
| 2 | Add `bootBuildImage` block (~35 lines) | 35 | Multi-arch builds break on ARM64 Macs |
| 3 | Fix image name in `function.yaml` to match convention | 1 | Image plan misses the function |
| 4 | Update `payloads/*.json` to match handler contract | ~5 | Contract tests fail |
| 5 | Sync `bootJar.archiveFileName` with `Dockerfile COPY` | 0-2 | Docker build fails with "jar not found" |
| 6 | Write a meaningful test | ~20 | Placeholder test passes without verifying anything |

Steps 1-3 are mechanical and should be generated. Steps 4-6 require judgment but can be improved with better placeholders.

## 3. Proposed improvements

### 3.1 Native image support (GraalVM plugin + bootBuildImage)

**Current template (`build.gradle`):**

```groovy
plugins {
    id 'org.springframework.boot' version "${springBootVersion}"
    id 'io.spring.dependency-management' version "${springDependencyManagementVersion}"
    id 'java'
}
// ... no bootBuildImage block
```

**Proposed template:**

Add the GraalVM plugin and the `bootBuildImage` block matching all existing Java functions. The block is identical across `word-stats`, `json-transform`, and `roman-numeral` — extract it into the template.

The `bootBuildImage` block handles:
- ARM64 Mac fallback builder (`dashaun/builder:tiny`)
- Platform auto-detection (`linux/arm64` vs `linux/amd64`)
- User overrides via `-PimageBuilder`, `-PimageRunImage`, `-PimagePlatform`
- Native image BP_NATIVE_IMAGE env

**Parameters to inject:**

- `imageName`: derived from language + function name → `nanofaas/java-{name}:buildpack`

### 3.2 Image name convention in function.yaml

**Current:** `image: nanofaas/{name}:latest`

**Proposed:** `image: nanofaas/{runtime-prefix}-{name}:latest`

Where `runtime-prefix` is:
- `java` for Java
- `java-lite` for Java Lite
- `python` for Python
- `go` for Go
- `javascript` for JavaScript
- `bash` for Bash

This matches the image plan convention in `nanolab/images/plan.py:_function_target`.

### 3.3 Payload contract alignment

**Current payloads** (`happy-path.json`):

```json
{
  "input": {"key": "value"},
  "expected": {"result": "ok"}
}
```

**Proposed:** Generate payloads that match the scaffolded handler's contract. Since fn-init knows the function name and language, it can infer reasonable inputs:

- Functions named after a transformation (e.g. `figlet`, `word-stats`): include a `text` field
- Generic: use the name as a hint, e.g. `{"text": "hello from {name}"}`

At minimum, annotate the payloads with a comment: `// Update after implementing your handler`.

### 3.4 Dockerfile ↔ build.gradle synchronization

**Current Dockerfile:**

```dockerfile
COPY build/libs/figlet.jar app.jar
```

If the user changes `archiveFileName` in `build.gradle`, the Dockerfile breaks with no warning.

**Proposed:** Add a comment in both files:

Dockerfile:
```dockerfile
# Must match bootJar.archiveFileName in build.gradle
COPY build/libs/figlet.jar app.jar
```

build.gradle:
```groovy
bootJar {
    archiveFileName = 'figlet.jar'  // keep in sync with Dockerfile COPY
}
```

Or generate a Gradle property and reference it from both.

### 3.5 Test template

**Current test:**

```java
@Test
void handleReturnsResult() {
    var req = new InvocationRequest(Map.of(), null);
    var result = handler.handle(req);
    assertNotNull(result);
}
```

**Proposed:** A test that verifies the handler returns the expected keys from the template handler contract:

```java
@Test
void handleReturnsExpectedKeys() {
    var req = new InvocationRequest(Map.of("text", "hello"), null);
    var result = handler.handle(req);
    assertNotNull(result);
    assertTrue(result instanceof Map, "handler must return a Map");
}
```

Plus a test for the error path (empty/missing input).

## 4. Template structure (after improvements)

```
functions/java/{name}/
├── Dockerfile          # with sync comment
├── build.gradle        # full plugins + bootBuildImage
├── function.yaml       # prefixed image name
├── payloads/
│   ├── happy-path.json # with contract hint
│   └── missing-input.json
└── src/
    ├── main/java/.../
    │   ├── {Name}Application.java
    │   └── {Name}Handler.java
    └── test/java/.../
        └── {Name}HandlerTest.java  # meaningful assertions
```

## 5. Out of scope

- Adding language-specific dependencies (e.g. `jfiglet`). The developer always needs to add domain dependencies manually.
- Generating the handler implementation. The scaffold provides the interface contract; business logic is the developer's responsibility.
- `settings.gradle` registration — already handled correctly.
- Non-Java language templates — each language has different conventions; evaluate separately.

## 6. Acceptance criteria

- [ ] Scaffolded `build.gradle` includes GraalVM plugin and `bootBuildImage` block
- [ ] `function.yaml` image name follows the `{runtime}-{name}` convention
- [ ] Dockerfile and build.gradle reference each other's jar name
- [ ] Payloads include a hint to update after implementation
- [ ] Test template verifies the handler returns a Map with expected keys
- [ ] An existing function scaffolded with the new template compiles and passes `./gradlew :functions:java:{name}:test`
