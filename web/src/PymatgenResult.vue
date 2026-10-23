<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ApiError, request } from "./api";

interface Crystal {
  formula: string; atom_count: number; volume_angstrom3: number; lattice_parameters: number[];
  lattice_matrix_angstrom: number[][]; symbols: string[]; fractional_positions: number[][];
  source?: { format?: string; original_fields?: Record<string, unknown> };
}
interface Peak { two_theta_degree: number; d_angstrom: number; intensity_raw_au: number;
  intensity_relative_max100: number; hkls: { hkl: number[]; multiplicity: number }[] }
interface ScientificResult {
  operation: string; structures: Crystal[]; input_structures: Crystal[]; parameters: Record<string, unknown>;
  details: { spacegroup?: { number: number; symbol: string; crystal_system: string }; peaks?: Peak[];
    matched?: boolean; rms_displacement_normalized?: number | null; max_displacement_normalized?: number | null };
}
const props = defineProps<{ jobId: string; userId: string; operation: string }>();
const emit = defineEmits<{ authError: [cause: unknown] }>();
const result = ref<ScientificResult | null>(null);
const loading = ref(false);
const error = ref("");
const sitePages = ref<Record<number, number>>({});
const peakPage = ref(0);
const pageSize = 20;
let serial = 0;
let alive = true;
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const vector = (value: unknown, size: number): value is number[] => Array.isArray(value) && value.length === size && value.every(finite);
function crystal(value: unknown): value is Crystal {
  if (!record(value) || typeof value.formula !== "string" || !Number.isInteger(value.atom_count)
      || Number(value.atom_count) < 1 || Number(value.atom_count) > 4096 || !finite(value.volume_angstrom3) || value.volume_angstrom3 <= 0
      || !vector(value.lattice_parameters, 6) || !Array.isArray(value.lattice_matrix_angstrom)
      || value.lattice_matrix_angstrom.length !== 3 || !value.lattice_matrix_angstrom.every(row => vector(row, 3))
      || !Array.isArray(value.symbols) || value.symbols.length !== value.atom_count || !value.symbols.every(symbol => typeof symbol === "string")
      || !Array.isArray(value.fractional_positions) || value.fractional_positions.length !== value.atom_count
      || !value.fractional_positions.every(point => vector(point, 3))) return false;
  return value.source === undefined || record(value.source) && (value.source.original_fields === undefined || record(value.source.original_fields));
}
function checked(value: unknown): ScientificResult {
  if (!record(value) || !record(value.result) || !record(value.parameters)) throw new Error("结果数据不完整，请重新读取。");
  const data = value.result;
  if (data.operation !== props.operation || !Array.isArray(data.structures) || !data.structures.length || data.structures.length > 2
      || !data.structures.every(crystal) || !Array.isArray(data.input_structures) || !data.input_structures.length
      || data.input_structures.length > 2 || !data.input_structures.every(crystal) || !record(data.details) || !record(data.parameters))
    throw new Error("结构结果不完整，请重新读取。");
  if (data.operation === "analyze" && (!record(data.details.spacegroup) || !Number.isInteger(data.details.spacegroup.number)
      || typeof data.details.spacegroup.symbol !== "string" || typeof data.details.spacegroup.crystal_system !== "string"))
    throw new Error("空间群结果不完整，请重新读取。");
  if (data.operation === "compare" && (typeof data.details.matched !== "boolean"
      || ![data.details.rms_displacement_normalized, data.details.max_displacement_normalized].every(value => value === null || finite(value) && value >= 0)))
    throw new Error("比较结果不完整，请重新读取。");
  if (data.operation === "xrd" && (!Array.isArray(data.details.peaks) || data.details.peaks.length > 5000 || !data.details.peaks.every(peak => record(peak)
      && [peak.two_theta_degree, peak.d_angstrom, peak.intensity_raw_au, peak.intensity_relative_max100].every(finite)
      && Array.isArray(peak.hkls) && peak.hkls.every(family => record(family) && Array.isArray(family.hkl)
        && [3, 4].includes(family.hkl.length) && family.hkl.every(Number.isInteger) && Number.isInteger(family.multiplicity)))))
    throw new Error("衍射峰表不完整，请重新读取。");
  return data as unknown as ScientificResult;
}
async function load() {
  const version = ++serial, id = props.jobId, owner = props.userId;
  result.value = null; error.value = ""; loading.value = true; sitePages.value = {}; peakPage.value = 0;
  try {
    const data = await request<unknown>(`/pymatgen/jobs/${encodeURIComponent(id)}/result`);
    if (!alive || serial !== version || props.userId !== owner || props.jobId !== id) return;
    result.value = checked(data);
  } catch (cause) {
    if (!alive || serial !== version || props.userId !== owner || props.jobId !== id) return;
    error.value = cause instanceof Error ? cause.message : "结果暂时无法读取。";
    if (cause instanceof ApiError && cause.status === 401) emit("authError", cause);
  } finally { if (alive && serial === version) loading.value = false; }
}
const number = (value: unknown) => !finite(value) ? "—" : value === 0 ? "0" :
  Math.abs(value) < 1e-5 || Math.abs(value) >= 1e7 ? value.toExponential(7) : Number(value.toPrecision(10)).toString();
const peaks = computed(() => result.value?.details.peaks ?? []);
const visiblePeaks = computed(() => peaks.value.slice(peakPage.value * pageSize, (peakPage.value + 1) * pageSize));
const hkl = (peak: Peak) => peak.hkls.map(family => `(${family.hkl.join(" ")}) ×${family.multiplicity}`).join("；");
const rows = (model: Crystal, index: number) => model.fractional_positions.slice((sitePages.value[index] ?? 0) * pageSize,
  ((sitePages.value[index] ?? 0) + 1) * pageSize).map((point, offset) => {
  const site = (sitePages.value[index] ?? 0) * pageSize + offset;
  return { index: site + 1, symbol: model.symbols[site], point };
});
const text = (value: unknown) => typeof value === "string" || finite(value) ? String(value) : "";
function declaration(model: Crystal) {
  const fields = model.source?.original_fields ?? {};
  return [fields['_space_group_name_h-m_alt'] ?? fields['_symmetry_space_group_name_h-m'],
    fields['_space_group_it_number'] ?? fields['_symmetry_int_tables_number']].map(text).filter(Boolean).join(" · ") ||
    (model.source?.format === "poscar" ? "POSCAR 未声明空间群" : "仅提供显式对称操作");
}
const crystalSystem = (value: string) => ({ cubic: "立方晶系", tetragonal: "四方晶系", orthorhombic: "正交晶系", hexagonal: "六方晶系",
  trigonal: "三方晶系", monoclinic: "单斜晶系", triclinic: "三斜晶系" } as Record<string, string>)[value] ?? value;
watch(() => [props.jobId, props.userId], () => void load(), { immediate: true });
onBeforeUnmount(() => { alive = false; ++serial; });
</script>

<template>
  <div class="scientific-result" aria-live="polite">
    <p v-if="loading" class="result-note">正在读取计算数据…</p>
    <div v-else-if="error" class="result-error"><p role="alert">{{ error }}</p><button type="button" class="text-button" @click="load">重新读取结果</button></div>
    <template v-else-if="result">
      <section v-if="result.operation === 'analyze' && result.details.spacegroup" class="result-section">
        <h4>空间群分析</h4>
        <p>{{ result.details.spacegroup.symbol }}（{{ result.details.spacegroup.number }}），{{ crystalSystem(result.details.spacegroup.crystal_system) }}。</p>
        <p class="result-note">位置容差 {{ number(result.parameters.symprec) }} Å；角度容差 {{ number(result.parameters.angle_tolerance) }}°。原文件声明与计算空间群分别保留。</p>
      </section>
      <section v-if="result.operation === 'compare'" class="result-section">
        <h4>结构比较条件</h4>
        <p>按元素和几何比较；约化原胞，保留实际体积，不匹配子集。</p>
        <p class="result-note">长度相对容差 {{ number(result.parameters.ltol) }}；位点容差 {{ number(result.parameters.stol) }}；角度容差 {{ number(result.parameters.angle_tol) }}°。</p>
        <dl v-if="result.details.matched" class="result-values"><div><dt>无量纲均方根位移</dt><dd>{{ number(result.details.rms_displacement_normalized) }}</dd></div><div><dt>无量纲最大位移</dt><dd>{{ number(result.details.max_displacement_normalized) }}</dd></div></dl>
        <p v-if="result.details.matched" class="result-note">位移以每原子平均自由长度归一化；匹配和均方根最优配对可能不同。</p>
      </section>
      <p v-if="result.operation === 'convert'" class="result-note">CIF 与 POSCAR 已往返核对元素、晶胞和周期坐标；附加动力学属性仅在原文件保留。</p>
      <p v-if="result.operation === 'supercell'" class="result-note">对角扩胞 {{ Array.isArray(result.parameters.scaling) ? result.parameters.scaling.join(' × ') : '—' }}：{{ result.input_structures[0]?.atom_count }} → {{ result.structures[0]?.atom_count }} 个原子。</p>
      <section v-for="(model, index) in result.structures" :key="index" class="result-section">
        <h4>{{ result.operation === 'compare' ? (index === 0 ? '结构 A' : '结构 B') : (result.operation === 'supercell' ? '超胞结构' : '结构数据') }} · {{ model.formula }}</h4>
        <dl class="result-values"><div><dt>晶胞长度 a / b / c（Å）</dt><dd>{{ model.lattice_parameters.slice(0, 3).map(number).join(' / ') }}</dd></div><div><dt>晶胞角度 α / β / γ（°）</dt><dd>{{ model.lattice_parameters.slice(3).map(number).join(' / ') }}</dd></div><div><dt>晶胞体积（Å³）</dt><dd>{{ number(model.volume_angstrom3) }}</dd></div><div><dt>原文件空间群声明</dt><dd>{{ declaration(model) }}</dd></div></dl>
        <details><summary>晶胞矩阵（Å）</summary><div class="table-scroll"><table><caption>三个晶格矢量</caption><thead><tr><th>矢量</th><th>x</th><th>y</th><th>z</th></tr></thead><tbody><tr v-for="(row, axis) in model.lattice_matrix_angstrom" :key="axis"><th>{{ ['a', 'b', 'c'][axis] }}</th><td v-for="(value, component) in row" :key="component">{{ number(value) }}</td></tr></tbody></table></div></details>
        <h5>原子分数坐标</h5>
        <div class="table-scroll"><table><caption>{{ result.operation === 'compare' ? (index === 0 ? '结构 A' : '结构 B') : '结构' }}的原子分数坐标</caption><thead><tr><th>序号</th><th>元素</th><th>x</th><th>y</th><th>z</th></tr></thead><tbody><tr v-for="site in rows(model, index)" :key="site.index"><td>{{ site.index }}</td><td>{{ site.symbol }}</td><td v-for="(value, component) in site.point" :key="component">{{ number(value) }}</td></tr></tbody></table></div>
        <nav v-if="model.atom_count > pageSize" class="result-pagination" :aria-label="`结构${index + 1}坐标分页`"><button type="button" class="text-button" :disabled="!(sitePages[index] ?? 0)" @click="sitePages[index] = (sitePages[index] ?? 0) - 1">上一页</button><span>第 {{ (sitePages[index] ?? 0) * pageSize + 1 }}–{{ Math.min(((sitePages[index] ?? 0) + 1) * pageSize, model.atom_count) }} / {{ model.atom_count }} 个原子</span><button type="button" class="text-button" :disabled="((sitePages[index] ?? 0) + 1) * pageSize >= model.atom_count" @click="sitePages[index] = (sitePages[index] ?? 0) + 1">下一页</button></nav>
      </section>
      <section v-if="result.operation === 'xrd'" class="result-section">
        <h4>XRD衍射峰表</h4><p class="result-note">波长 {{ number(result.parameters.wavelength_angstrom) }} Å；2θ {{ number(result.parameters.two_theta_min) }}–{{ number(result.parameters.two_theta_max) }}°。未归一化强度采用模型任意单位，相对强度按本区间最大值为100计算；未自动精修结构。</p>
        <p v-if="!peaks.length">所选角度范围内没有可分辨衍射峰。</p>
        <template v-else><div class="table-scroll"><table><caption>理论衍射峰，含模型强度与相对强度</caption><thead><tr><th>2θ（°）</th><th>d（Å）</th><th>未归一化强度（a.u.）</th><th>相对强度</th><th>hkl（多重性）</th></tr></thead><tbody><tr v-for="(peak, index) in visiblePeaks" :key="index"><td>{{ number(peak.two_theta_degree) }}</td><td>{{ number(peak.d_angstrom) }}</td><td>{{ number(peak.intensity_raw_au) }}</td><td>{{ number(peak.intensity_relative_max100) }}</td><td>{{ hkl(peak) }}</td></tr></tbody></table></div><nav v-if="peaks.length > pageSize" class="result-pagination" aria-label="XRD衍射峰表分页"><button type="button" class="text-button" :disabled="peakPage === 0" @click="peakPage--">上一页</button><span>第 {{ peakPage * pageSize + 1 }}–{{ Math.min((peakPage + 1) * pageSize, peaks.length) }} / {{ peaks.length }} 个峰</span><button type="button" class="text-button" :disabled="(peakPage + 1) * pageSize >= peaks.length" @click="peakPage++">下一页</button></nav></template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.scientific-result { min-width: 0; margin-top: 20px; }
.result-section { border-top: 1px solid #eee8ed; padding: 18px 0 4px; min-width: 0; }
h4 { margin: 0 0 12px; font-size: 16px; overflow-wrap: anywhere; } h5 { margin: 18px 0 10px; font-size: 14px; }
p { font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }
.result-note { color: #827b86; font-size: 12px; }
.result-error { color: #a3344e; }
.result-values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.result-values dt { font-size: 12px; color: #827b86; margin-bottom: 5px; }
.result-values dd { font-size: 13px; margin: 0; overflow-wrap: anywhere; }
details { font-size: 13px; margin: 16px 0; } summary { cursor: pointer; }
.table-scroll { max-width: 100%; overflow-x: auto; margin: 12px 0; }
table { border-collapse: collapse; min-width: 420px; width: 100%; font-size: 12px; text-align: left; font-variant-numeric: tabular-nums; }
caption { text-align: left; color: #827b86; padding: 0 0 8px; }
th, td { padding: 10px 8px; border-bottom: 1px solid #eee8ed; white-space: nowrap; }
th { color: #67606e; background: #fdf8fa; font-weight: 600; }
.result-pagination { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
@media (max-width: 600px) { .result-values { grid-template-columns: 1fr; } }
</style>
