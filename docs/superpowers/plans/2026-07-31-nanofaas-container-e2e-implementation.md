# Piano: Rimozione test E2E Java da NanoFaaS

> **Per agenti:** Usare superpowers:subagent-driven-development. Checkbox (`- [ ]`) per tracciamento.

**Obiettivo:** Rimuovere i test E2E Java da NanoFaaS. La specifica in NanoLab ne documenta il comportamento.

**Criterio:** Un test Java può essere eliminato quando in NanoLab esiste la descrizione del workflow corrispondente. La specifica `docs/superpowers/specs/2026-07-31-nanofaas-e2e-workflows-in-nanolab.md` copre tutti i test.

---

## Stato attuale

| Test Java | Workflow NanoLab | Stato |
|-----------|-----------------|-------|
| `ContainerLocalE2eTest` | `validate-container` (§5.1) | Implementato |
| `E2eFlowTest` | `validate-container` + `validate-k8s` (§5.1, §5.2) | Implementato |
| `K8sE2eTest` | `validate-k8s` (§5.2) | Implementato |
| `BuildpackE2eTest` | `validate-buildpack` (§5.5) | Specifica |
| `SdkExamplesE2eTest` | `validate-sdk-examples` (§5.6) | Specifica |

---

## Task 1 — Rimuovere test e classi da NanoFaaS

### 1.1 ContainerLocalE2eTest

- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/ContainerLocalE2eTest.java`

### 1.2 E2eFlowTest

- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/E2eFlowTest.java`

### 1.3 K8sE2eTest e classi associate

- [ ] Rimuovere `platform/modules/k8s-deployment-provider/src/test/java/.../e2e/K8sE2eTest.java`
- [ ] Rimuovere `platform/modules/k8s-deployment-provider/src/test/java/.../e2e/K8sE2eScenarioManifest.java`
- [ ] Rimuovere `platform/modules/k8s-deployment-provider/src/test/java/.../e2e/K8sE2eScenarioManifestTest.java`
- [ ] Rimuovere `platform/modules/k8s-deployment-provider/src/test/java/.../e2e/K8sE2eScenarioManifestCommandTest.java`
- [ ] Rimuovere `platform/modules/k8s-deployment-provider/src/test/java/.../e2e/K8sE2eDeploymentSpecTest.java`

### 1.4 BuildpackE2eTest

- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/BuildpackE2eTest.java`
- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/BuildpackE2eCommandTest.java`

### 1.5 SdkExamplesE2eTest

- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/SdkExamplesE2eTest.java`

### 1.6 E2eApiSupport e E2eTestSupport

- [ ] Verificare se `E2eApiSupport.java` e `E2eTestSupport.java` sono ancora referenziati da test rimasti
- [ ] Se non più usati, rimuovere `platform/control-plane/src/test/java/.../e2e/E2eApiSupport.java`
- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/E2eTestSupport.java`
- [ ] Rimuovere `E2eApiSupport.java` dai testFixtures se presente

### 1.7 E2eApiSupportTest e E2eTestSupportTest

- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/E2eApiSupportTest.java`
- [ ] Rimuovere `platform/control-plane/src/test/java/.../e2e/E2eTestSupportTest.java`

---

## Task 2 — Pulizia Gradle in NanoFaaS

- [ ] Se nessun test usa più `@Tag("inter_e2e")`, rimuovere il blocco `excludeTags` dal root `build.gradle`:

```groovy
// Da rimuovere:
tasks.withType(Test).configureEach {
    useJUnitPlatform {
        if (!project.hasProperty('runE2e')) {
            excludeTags 'inter_e2e'
        }
    }
}
```

- [ ] Rimuovere la property `runE2e` se non più referenziata
- [ ] Rimuovere eventuali riferimenti a `inter_e2e` in `jacoco` o altre configurazioni

---

## Task 3 — Nessuna modifica a NanoLab

Il meccanismo `run_java_e2e` in `PlatformRequest` e il task `K8sE2eTest` in `add_platform` restano. Sono codice funzionante che:

- dimostra come NanoLab invoca test taggati di NanoFaaS;
- verrà riutilizzato quando i workflow black-box sostituiranno i test Java;
- fallirà naturalmente a runtime se la classe Java non esiste più — il fallimento è visibile e intenzionale, non nascosto.

La pulizia di `platform.py` va fatta solo quando il workflow `validate-k8s` avrà asserzioni black-box equivalenti (piano separato).

---

## Task 4 — Verifica

- [ ] `./gradlew test --no-parallel` in NanoFaaS passa
- [ ] `NANOFAAS_ROOT=... uv run --package nanolab pytest` in NanoLab passa
- [ ] `grep -rn "inter_e2e\|E2eTest\|E2eApiSupport"` in NanoFaaS non trova riferimenti attivi
- [ ] `grep -rn "scripts/controlplane.sh\|k8sE2e\|k8sE2eVm"` in NanoFaaS non trova riferimenti attivi

---

## Cosa NON fare

- Non implementare `validate-buildpack` o `validate-sdk-examples` (la specifica basta)
- Non toccare `experiments/**`, `docs/plans/**`, snapshot o materiale storico
- Non modificare release, build immagini, benchmark
