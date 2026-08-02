# fn-init Java Template Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the fn-init Java template to match existing Java function conventions, eliminating 6 manual steps the developer currently performs after scaffolding.

**Architecture:** 4 template files + 1 placeholder + 1 payload generation change. All changes are in `tools/fn-init/src/fn_init/`. No new files.

**Tech Stack:** Python 3.12+, Jinja-free (manual `{{PLACEHOLDER}}` replacement)

## Global Constraints

- Target repo: `miciav/nanofaas` (tool at `tools/fn-init`)
- Existing tests at `tools/fn-init/tests/` must pass after all changes
- Placeholder syntax: `{{PLACEHOLDER}}` (already used by the codebase)
- IMAGE_TAG format: `nanofaas/{runtime}-{name}:latest`
- Runtime prefix for Java: `java`
- bootBuildImage block is identical across all existing Java functions — copy it verbatim from `functions/java/word-stats/build.gradle` (lines 20-47)
- Do NOT modify the handler template (Handler.java.tmpl) — business logic is the developer's responsibility
- Do NOT modify settings.gradle logic — already correct
- ./gradlew test must pass after scaffolding a test function

---

### Task 1: Add GraalVM plugin and bootBuildImage block to build.gradle template

**Files:**
- Modify: `tools/fn-init/src/fn_init/templates/java/build.gradle.tmpl`

**Interfaces:**
- Consumes: `{{FUNCTION_NAME}}` placeholder (already provided)
- Produces: complete `build.gradle` matching existing Java functions

- [ ] **Step 1: Replace the build.gradle template**

Read the current template at `tools/fn-init/src/fn_init/templates/java/build.gradle.tmpl`. Replace its entire content with:

```groovy
plugins {
    id 'org.springframework.boot' version "${springBootVersion}"
    id 'io.spring.dependency-management' version "${springDependencyManagementVersion}"
    id 'org.graalvm.buildtools.native' version "${graalvmBuildToolsVersion}"
    id 'java'
}

dependencies {
    implementation project(':sdks:java')
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
}

tasks.named('test') {
    useJUnitPlatform()
}

bootJar {
    archiveFileName = '{{FUNCTION_NAME}}.jar'
}

bootBuildImage {
    imageName = project.findProperty('functionImage') ?: 'nanofaas/java-{{FUNCTION_NAME}}:buildpack'
    def isArm = System.getProperty('os.arch') in ['aarch64', 'arm64']
    def defaultBuilder = isArm ? 'dashaun/builder:tiny' : 'paketobuildpacks/builder-jammy-tiny:latest'
    def builderImage = (project.findProperty('imageBuilder') ?: System.getenv('IMAGE_BUILDER')
            ?: defaultBuilder).toString()
    def runImageName = project.findProperty('imageRunImage') ?: System.getenv('IMAGE_RUN_IMAGE')
    if (!runImageName && !isArm) {
        runImageName = 'paketobuildpacks/run-jammy-tiny:latest'
    }
    builder = builderImage
    if (runImageName) {
        runImage = runImageName.toString()
    }
    def hostArch = System.getProperty('os.arch')
    def defaultPlatform = (hostArch in ['aarch64', 'arm64']) ? 'linux/arm64' : 'linux/amd64'
    def targetPlatform = project.findProperty('imagePlatform') ?: System.getenv('IMAGE_PLATFORM') ?: defaultPlatform
    imagePlatform = targetPlatform
    environment = ['BP_NATIVE_IMAGE': 'true']
}
```

- [ ] **Step 2: Verify the template renders correctly**

Run: `uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes && cat /tmp/test-func/build.gradle`

Expected: the output matches the template above with `test-func` substituted for `{{FUNCTION_NAME}}`.

- [ ] **Step 3: Scaffold a real function and run ./gradlew test**

```bash
rm -rf /tmp/test-func
uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes
cd /tmp && cp -r /Users/micheleciavotta/Downloads/mcFaas/sdks /tmp/sdks 2>/dev/null || true
```

Then run `./gradlew :functions:java:test-func:test` if inside the monorepo, or inspect the output.

- [ ] **Step 4: Commit**

```bash
git add tools/fn-init/src/fn_init/templates/java/build.gradle.tmpl
git commit -m "feat(fn-init): add GraalVM plugin and bootBuildImage to Java template"
```

---

### Task 2: Add language-prefixed IMAGE_TAG placeholder

**Files:**
- Modify: `tools/fn-init/src/fn_init/main.py:67-72`

**Interfaces:**
- Consumes: `name` (function name), `class_name`, `package` (already available)
- Produces: `IMAGE_TAG = f"nanofaas/{prefix}-{name}:latest"` where prefix derives from `lang`

- [ ] **Step 1: Read the current placeholder block**

Read `tools/fn-init/src/fn_init/main.py` lines 60-75 to see the `placeholders` dict construction.

- [ ] **Step 2: Update the IMAGE_TAG placeholder**

Find line 72:
```python
"IMAGE_TAG": f"nanofaas/{name}:latest",
```

Replace with:
```python
"IMAGE_TAG": f"nanofaas/java-{name}:latest" if lang == "java" else f"nanofaas/{name}:latest",
```

- [ ] **Step 3: Verify the rendered function.yaml**

Run: `uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes && grep image: /tmp/test-func/function.yaml`

Expected: `image: nanofaas/java-test-func:latest`

- [ ] **Step 4: Commit**

```bash
git add tools/fn-init/src/fn_init/main.py
git commit -m "feat(fn-init): use java- prefix in IMAGE_TAG for Java functions"
```

---

### Task 3: Add Dockerfile/build.gradle sync comments

**Files:**
- Modify: `tools/fn-init/src/fn_init/templates/java/Dockerfile.tmpl`
- Modify: `tools/fn-init/src/fn_init/templates/java/build.gradle.tmpl`

- [ ] **Step 1: Add comment to Dockerfile template**

Read `tools/fn-init/src/fn_init/templates/java/Dockerfile.tmpl`. Insert a comment line before the COPY:

```dockerfile
FROM eclipse-temurin:21-jre
WORKDIR /app
# Must match bootJar.archiveFileName in build.gradle
COPY build/libs/{{FUNCTION_NAME}}.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
```

- [ ] **Step 2: Add comment to build.gradle template**

In the `bootJar` block of `build.gradle.tmpl` (already modified in Task 1), add the comment:

```groovy
bootJar {
    archiveFileName = '{{FUNCTION_NAME}}.jar'  // keep in sync with Dockerfile COPY
}
```

- [ ] **Step 3: Verify**

Run: `uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes && head -4 /tmp/test-func/Dockerfile && grep archiveFileName /tmp/test-func/build.gradle`

Expected: Dockerfile has the sync comment, build.gradle has the inline comment.

- [ ] **Step 4: Commit**

```bash
git add tools/fn-init/src/fn_init/templates/java/Dockerfile.tmpl tools/fn-init/src/fn_init/templates/java/build.gradle.tmpl
git commit -m "docs(fn-init): add Dockerfile/build.gradle sync comments to Java template"
```

---

### Task 4: Improve handler test template

**Files:**
- Modify: `tools/fn-init/src/fn_init/templates/java/HandlerTest.java.tmpl`

- [ ] **Step 1: Replace the test template**

Read `tools/fn-init/src/fn_init/templates/java/HandlerTest.java.tmpl`. Replace the `handleReturnsResult` test method with two tests:

```java
package {{PACKAGE}};

import it.unimib.datai.nanofaas.common.model.InvocationRequest;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class {{CLASS_NAME}}HandlerTest {

    private final {{CLASS_NAME}}Handler handler = new {{CLASS_NAME}}Handler();

    @Test
    void handleReturnsMapWithResult() {
        var req = new InvocationRequest(Map.of("text", "hello"), null);
        var result = handler.handle(req);
        assertNotNull(result, "handler must return a non-null response");
        assertInstanceOf(Map.class, result, "handler must return a Map");
    }

    @Test
    void handleRejectsEmptyInput() {
        var req = new InvocationRequest(Map.of(), null);
        var result = handler.handle(req);
        assertNotNull(result);
        @SuppressWarnings("unchecked")
        Map<String, Object> map = (Map<String, Object>) result;
        assertTrue(map.containsKey("error"), "empty input should return an error key");
    }
}
```

- [ ] **Step 2: Verify the test compiles**

Run: `uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes && cat /tmp/test-func/src/test/java/it/unimib/datai/nanofaas/examples/testfunc/TestFuncHandlerTest.java`

Expected: two tests, `handleReturnsMapWithResult` and `handleRejectsEmptyInput`.

- [ ] **Step 3: Commit**

```bash
git add tools/fn-init/src/fn_init/templates/java/HandlerTest.java.tmpl
git commit -m "feat(fn-init): improve Java handler test with meaningful assertions"
```

---

### Task 5: Add payload contract hints

**Files:**
- Modify: `tools/fn-init/src/fn_init/generator.py:239-252`

- [ ] **Step 1: Update the payload generation**

Read `tools/fn-init/src/fn_init/generator.py` lines 239-252. Replace the payload dicts:

```python
    payloads_dir = output_dir / "payloads"
    (payloads_dir / "assets").mkdir(parents=True, exist_ok=True)
    happy = {
        "_comment": "Update after implementing your handler — see Handler.java.tmpl for the expected contract",
        "description": f"invoke {name} with valid input",
        "input": {"text": "hello"},
        "expected": {"result": "ok"},
    }
    missing = {
        "_comment": "Update after implementing your handler — see Handler.java.tmpl for the expected contract",
        "description": f"invoke {name} with empty input",
        "input": {},
        "expected": {"error": "Field 'text' is required and must be non-empty"},
    }
    (payloads_dir / "happy-path.json").write_text(json.dumps(happy, indent=2))
    (payloads_dir / "missing-input.json").write_text(json.dumps(missing, indent=2))
    created += [payloads_dir / "happy-path.json", payloads_dir / "missing-input.json"]
```

- [ ] **Step 2: Verify**

Run: `uv run --project tools/fn-init fn-init test-func --lang java --out /tmp --yes && cat /tmp/test-func/payloads/happy-path.json`

Expected: contains `_comment` field and `"text": "hello"` as input.

- [ ] **Step 3: Run existing fn-init tests**

Run: `uv run --project tools/fn-init pytest tools/fn-init/tests/ -v`

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add tools/fn-init/src/fn_init/generator.py
git commit -m "feat(fn-init): add contract hints to generated payloads"
```

---

### Task 6: End-to-end validation

- [ ] **Step 1: Scaffold a fresh Java function in the monorepo**

```bash
uv run --project tools/fn-init fn-init e2e-test-func --lang java --yes
```

- [ ] **Step 2: Verify all generated files**

Check each file:

| File | Check |
|------|-------|
| `build.gradle` | Has GraalVM plugin + bootBuildImage block + sync comment |
| `function.yaml` | `image: nanofaas/java-e2e-test-func:latest` |
| `Dockerfile` | Has sync comment above COPY |
| `HandlerTest.java` | Two test methods with Map assertions |
| `happy-path.json` | Has `_comment` + `"text": "hello"` |
| `missing-input.json` | Has `_comment` + `"error": "..."` |

- [ ] **Step 3: Build and test**

```bash
./gradlew :functions:java:e2e-test-func:test
./gradlew :functions:java:e2e-test-func:bootJar
```

Expected: `BUILD SUCCESSFUL` for both commands. The test from Task 4 will fail because the template handler returns `{"result": "ok"}` rather than `{"error": "..."}` on empty input — this is expected and demonstrates that the developer must implement the handler. The `bootJar` must succeed (26+ MB jar produced).

- [ ] **Step 4: Clean up the test function**

```bash
# Revert settings.gradle change
git checkout settings.gradle
rm -rf functions/java/e2e-test-func
```

- [ ] **Step 5: Commit**

```bash
# Nothing to commit — the test function is cleaned up
# The previous commits contain all the changes
```
