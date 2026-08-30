"""
Cognitive Audit & Technical Quality Passport Generator.

This module performs a purely algorithmic (zero-hallucination) analysis of
long-term memory (LTM) and short-term memory (STM) in biomem and generates
a professional, officially clean and elegant PDF report for Practice-as-a-Product (PaaP).

Contents:
1. CognitiveAuditAnalyzer — mathematical and topological analysis (Ward clustering,
   TF-IDF keywords, semantic density, top anchors).
2. CognitiveReportPDFGenerator — rendering of the HTML/CSS template and PDF export
   using PyQt6 QTextDocument and QPrinter.
"""
import math
import re
import datetime
import logging
from typing import Dict, Any, List, Optional
import base64
from io import BytesIO

import torch
import numpy as np
from .localization import T

try:
    from scipy.cluster.hierarchy import linkage, fcluster
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False

logger = logging.getLogger('memory_module.cognitive_audit')

_STOP_WORDS = {'být', 'těch', 'für', 'při', 'vše', 'když', 'mají', 'není', 'přes', 'však', 'über', 'která', 'které', 'který', 'může', 'co', 'all', 'and', 'any', 'are', 'auf', 'but', 'can', 'das', 'dem', 'den', 'der', 'des', 'die', 'for', 'has', 'jak', 'kde', 'kdo', 'mit', 'not', 'one', 'pro', 'tak', 'tam', 'ten', 'the', 'und', 'von', 'was', 'who', 'you', 'aber', 'auch', 'been', 'bude', 'byla', 'bylo', 'eine', 'from', 'have', 'into', 'jako', 'jeho', 'jsem', 'jsme', 'jsou', 'kann', 'mezi', 'more', 'nebo', 'oder', 'over', 'sich', 'sind', 'tady', 'tato', 'that', 'this', 'tyto', 'were', 'what', 'when', 'will', 'wird', 'with', 'your', 'about', 'after', 'could', 'einer', 'eines', 'nicht', 'tento', 'their', 'there', 'which', 'would', 'jejich', 'should', 'werden'}


class CognitiveAuditAnalyzer:
    """
    Performs the algorithmic analysis of the TextMemory module.
    Returns structured data for building the Cognitive audit.
    """

    def __init__(self, memory: Any):
        self.memory = memory

    def analyze(self) -> Dict[str, Any]:
        """
        Runs the complete analysis and returns a dictionary with the results.
        """
        vital_metrics = self._compute_vital_metrics()
        domain_clusters, linkage_matrix, active_indices, K_active = self._compute_domain_clusters()
        network_metrics = self._compute_network_metrics(active_indices, K_active)
        kinetics_metrics = self._compute_kinetics()
        top_anchors = self._compute_top_anchors()
        chart_base64 = self._render_dendrogram_base64(linkage_matrix)
        return {'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'vital_metrics': vital_metrics, 'domain_clusters': domain_clusters, 'network_metrics': network_metrics, 'kinetics_metrics': kinetics_metrics, 'top_anchors': top_anchors, 'chart_base64': chart_base64, '_linkage_matrix': linkage_matrix, '_active_indices': active_indices}

    def _render_dendrogram_base64(self, Z: Any) -> Optional[str]:
        """
        Renders the dendrogram into a base64 PNG via matplotlib, if available.
        """
        if Z is None or not _SCIPY_AVAILABLE:
            return None
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            from scipy.cluster.hierarchy import dendrogram
            fig, ax = plt.subplots(figsize=(6.5, 2.8), dpi=130)
            dendrogram(Z, ax=ax, no_labels=True, above_threshold_color='#64748b')
            ax.set_title(T('report.dendrogram_chart_title'), fontsize=10, color='#1e293b', pad=10)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color('#cbd5e1')
            ax.spines['bottom'].set_color('#cbd5e1')
            ax.tick_params(colors='#64748b', labelsize=8)
            plt.tight_layout()
            buf = BytesIO()
            fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
            plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
        except Exception as e:
            logger.debug('Matplotlib dendrogram rendering failed: %s', e)
            return None

    def _get_active_ltm_records(self) -> List[Dict[str, Any]]:
        records = []
        if hasattr(self.memory, 'ltm_centers') and self.memory.ltm_centers is not None:
            ltm = self.memory.ltm_centers
            n = getattr(ltm, 'n_centers', 0)
            for i in range(n):
                active_flag = ltm.active[i] if hasattr(ltm, 'active') and i < len(ltm.active) else False
                if isinstance(active_flag, torch.Tensor):
                    active_flag = bool(active_flag.item())
                else:
                    active_flag = bool(active_flag)
                if not active_flag:
                    continue
                key_t = ltm.key_texts[i] if hasattr(ltm, 'key_texts') and i < len(ltm.key_texts) and ltm.key_texts[i] is not None else ''
                val_t = ltm.value_texts[i] if hasattr(ltm, 'value_texts') and i < len(ltm.value_texts) and ltm.value_texts[i] is not None else ''
                int_v = ltm.h[i].item() if hasattr(ltm, 'h') and i < len(ltm.h) and hasattr(ltm.h[i], 'item') else float(ltm.h[i] if hasattr(ltm, 'h') and i < len(ltm.h) else 1)
                acc_v = ltm.usage[i].item() if hasattr(ltm, 'usage') and i < len(ltm.usage) and hasattr(ltm.usage[i], 'item') else int(ltm.usage[i] if hasattr(ltm, 'usage') and i < len(ltm.usage) else 0)
                age_v = ltm.age[i].item() if hasattr(ltm, 'age') and i < len(ltm.age) and hasattr(ltm.age[i], 'item') else int(ltm.age[i] if hasattr(ltm, 'age') and i < len(ltm.age) else 0)
                vec = ltm.K[i] if hasattr(ltm, 'K') and ltm.K is not None and i < len(ltm.K) else None
                records.append({'index': i, 'key_text': key_t, 'value_text': val_t, 'intensity': float(int_v), 'access_count': int(acc_v), 'age': int(age_v), 'embedding': vec})
        elif hasattr(self.memory, 'centers') and self.memory.centers is not None:
            for idx, c in enumerate(self.memory.centers):
                if getattr(c, 'active', True) or getattr(c, 'intensity', 0) > 0:
                    records.append({'index': idx, 'key_text': getattr(c, 'key_text', '') or getattr(c, 'stored_text', '') or '', 'value_text': getattr(c, 'value_text', '') or '', 'intensity': float(getattr(c, 'intensity', 1)), 'access_count': int(getattr(c, 'access_count', 0)), 'age': int(getattr(c, 'age', 0)), 'embedding': getattr(c, 'embedding', None)})
        return records

    def _compute_vital_metrics(self) -> Dict[str, Any]:
        records = self._get_active_ltm_records()
        ltm_count = len(records)
        if hasattr(self.memory, 'stm_centers') and self.memory.stm_centers is not None:
            stm_count = getattr(self.memory.stm_centers, 'get_n_active', lambda: 0)()
        elif hasattr(self.memory, 'stm_records'):
            stm_count = len(self.memory.stm_records)
        else:
            stm_count = 0
        total_records = max(ltm_count, stm_count)
        consolidation_ratio = min(1, ltm_count / total_records) if total_records > 0 else 0
        avg_intensity = 0
        avg_access = 0
        ages = []
        if ltm_count > 0:
            intensities = [r['intensity'] for r in records]
            accesses = [r['access_count'] for r in records]
            ages = [r['age'] for r in records]
            avg_intensity = sum(intensities) / len(intensities) if intensities else 0
            avg_access = sum(accesses) / len(accesses) if accesses else 0
        min_age = min(ages) if ages else 0
        max_age = max(ages) if ages else 0
        return {'ltm_count': ltm_count, 'stm_count': stm_count, 'total_records': total_records, 'consolidation_ratio': consolidation_ratio, 'avg_intensity': avg_intensity, 'avg_access_count': avg_access, 'read_count': getattr(self.memory, 'read_count', 0), 'write_count': getattr(self.memory, 'write_count', 0), 'consolidation_count': getattr(self.memory, 'consolidation_count', 0), 'min_age': min_age, 'max_age': max_age}

    def _compute_domain_clusters(self):
        """
        Clusters the active LTM centers using Ward's method and extracts
        keywords (domains).
        """
        records = self._get_active_ltm_records()
        if not records:
            return ([], None, [], None)
        vectors = []
        texts = []
        intensities = []
        indices = []
        for rec in records:
            vec = rec['embedding']
            if vec is None or not isinstance(vec, torch.Tensor):
                continue
            vectors.append(vec.cpu().detach().float().squeeze(0) if vec.dim() > 1 else vec.cpu().detach().float())
            texts.append(f"{rec['key_text']} {rec['value_text']}".strip())
            intensities.append(rec['intensity'])
            indices.append(rec['index'])
        n_active = len(vectors)
        if n_active < 2 or not _SCIPY_AVAILABLE:
            if n_active == 1:
                return ([{'cluster_id': 1, 'size': 1, 'share': 100, 'keywords': self._extract_keywords([texts[0]], top_n=4), 'avg_intensity': intensities[0]}], None, indices, torch.stack(vectors) if vectors else None)
            return ([], None, indices, None)
        K_active = torch.stack(vectors).numpy()
        Z = linkage(K_active, method='ward')
        target_k = max(2, min(7, n_active // 3))
        cluster_labels = fcluster(Z, t=target_k, criterion='maxclust')
        cluster_map = {}
        for i, label in enumerate(cluster_labels):
            cluster_map.setdefault(label, []).append(i)
        clusters_info = []
        for cid, i_list in cluster_map.items():
            c_texts = [texts[i] for i in i_list]
            c_intensities = [intensities[i] for i in i_list]
            keywords = self._extract_keywords(c_texts, top_n=4)
            avg_int = sum(c_intensities) / len(c_intensities) if c_intensities else 0
            share = len(i_list) / n_active * 100
            clusters_info.append({'cluster_id': int(cid), 'size': len(i_list), 'share': share, 'keywords': keywords, 'avg_intensity': avg_int})
        clusters_info.sort(key=lambda x: x['share'], reverse=True)
        return (clusters_info, Z, indices, torch.stack(vectors))

    def _extract_keywords(self, texts: List[str], top_n: int = 4) -> str:
        """
        Purely algorithmic frequency extraction of words with stop word and number filtering.
        """
        word_counts = {}
        for text in texts:
            words = re.findall(r'\b[a-zA-Zá-žÁ-Ž]{3,}\b', text.lower())
            for w in words:
                if w not in _STOP_WORDS and not w.isdigit():
                    word_counts[w] = word_counts.get(w, 0) + 1
        if not word_counts:
            return T('report.general_content')
        sorted_words = sorted(word_counts.items(), key=lambda x: (x[1], len(x[0])), reverse=True)
        top_words = [w[0].capitalize() for w in sorted_words[:top_n]]
        return ', '.join(top_words)

    def _compute_network_metrics(self, active_indices: List[int], K_active: Optional[torch.Tensor]) -> Dict[str, Any]:
        """
        Computes the graph topology of semantic relations (density and nodes/bridges).
        It also includes the Clustering coefficient and Eigenvector Centrality.
        """
        if K_active is None or K_active.size(0) < 2:
            return {'density': 0, 'density_label': T('report.density_low_iso', default='Low (isolated records)'), 'clustering': 0, 'eigen_hubs': [], 'hubs': []}
        norms = torch.norm(K_active, p=2, dim=1, keepdim=True)
        norms = torch.where(norms == 0, torch.ones_like(norms), norms)
        K_norm = K_active / norms
        sim_matrix = (K_norm @ K_norm.T).cpu().detach().numpy()
        n = sim_matrix.shape[0]
        np.fill_diagonal(sim_matrix, 0)
        threshold = 0.55
        A = (sim_matrix >= threshold).astype(float)
        edges = np.sum(A) // 2
        max_edges = n * (n - 1) / 2
        density = edges / max_edges if max_edges > 0 else 0
        if density > 0.4:
            d_label = T('report.density_high', default='High (compact knowledge network)')
        elif density > 0.18:
            d_label = T('report.density_medium', default='Medium (structured connections)')
        else:
            d_label = T('report.density_low_spec', default='Low (isolated specifics)')
        degrees = np.sum(A, axis=1)
        A3 = np.linalg.matrix_power(A, 3)
        C_i = np.zeros(n)
        for i in range(n):
            if degrees[i] >= 2:
                C_i[i] = A3[(i, i)] / (degrees[i] * (degrees[i] - 1))
        clustering_coef = float(np.mean(C_i)) if n > 0 else 0
        v = np.ones(n) / np.sqrt(n) if n > 0 else np.zeros(0)
        for _ in range(50):
            if n == 0:
                break
            v_new = A @ v
            norm = np.linalg.norm(v_new)
            if norm == 0:
                break
            v = v_new / norm
        eigen_hubs_idx = np.argsort(v)[::-1][:3]
        eigen_hubs = []
        rec_by_idx = {r['index']: r for r in self._get_active_ltm_records()}
        for idx in eigen_hubs_idx:
            if v[idx] > 0.01:
                real_center_idx = active_indices[idx]
                r = rec_by_idx.get(real_center_idx, {})
                key_t = r.get('key_text', '') or r.get('value_text', '') or ''
                short_text = key_t[:60] + '...' if len(key_t) > 60 else key_t
                eigen_hubs.append({'center_id': real_center_idx, 'score': float(v[idx]), 'text': short_text or T('report.center_id', default='Center #{}').format(real_center_idx)})
        return {'density': density, 'density_label': d_label, 'clustering': clustering_coef, 'eigen_hubs': eigen_hubs, 'hubs': []}

    def _compute_kinetics(self) -> Dict[str, Any]:
        """
        Computes the memory kinetics and the semantic drift of the centroid over time.
        """
        records = self._get_active_ltm_records()
        n = len(records)
        if n < 5:
            return {'drift': 0, 'status': T('report.drift_min_data', default='Insufficient data to measure drift (min. 5 memories)')}
        records_sorted = sorted(records, key=lambda x: x['index'])
        split_idx = int(n * 0.8)
        if split_idx == 0:
            split_idx = 1
        older_embeddings = []
        all_embeddings = []
        for i, r in enumerate(records_sorted):
            vec = r['embedding']
            if vec is None:
                continue
            v = vec.cpu().detach().float().squeeze(0)
            all_embeddings.append(v)
            if i < split_idx:
                older_embeddings.append(v)
        if len(older_embeddings) == 0 or len(all_embeddings) == 0:
            return {'drift': 0, 'status': T('report.drift_nodata', default='Insufficient vector data')}
        older_tensor = torch.stack(older_embeddings)
        all_tensor = torch.stack(all_embeddings)
        mu_t1 = torch.mean(older_tensor, dim=0)
        mu_t = torch.mean(all_tensor, dim=0)
        cos_sim = torch.nn.functional.cosine_similarity(mu_t1.unsqueeze(0), mu_t.unsqueeze(0)).item()
        drift = 1 - cos_sim
        if drift < 0.02:
            status = T('report.drift_stable', default='Stable (negligible semantic shift)')
        elif drift < 0.08:
            status = T('report.drift_incremental', default='Gradual evolution (incremental knowledge evolution)')
        else:
            status = T('report.drift_dynamic', default='Dynamic evolution (significant shift of the memory centroid)')
        return {'drift': drift, 'status': status}

    def _compute_top_anchors(self) -> List[Dict[str, Any]]:
        """
        Gets the 10 most valuable centers by composite score (intensity * log(1+access)).
        """
        records = self._get_active_ltm_records()
        if not records:
            return []
        scored_centers = []
        for rec in records:
            int_v = rec['intensity']
            acc_v = rec['access_count']
            score = int_v * (1 + math.log(1 + acc_v))
            key_t = rec['key_text'] or rec['value_text'] or ''
            short_t = key_t[:70] + '...' if len(key_t) > 70 else key_t
            scored_centers.append({'center_id': rec['index'], 'score': score, 'intensity': int_v, 'access_count': acc_v, 'text': short_t or T('report.center_id').format(rec['index'])})
        scored_centers.sort(key=lambda x: x['score'], reverse=True)
        return scored_centers[:10]


class CognitiveReportPDFGenerator:
    """
    Renders the Cognitive audit into an official, clean and elegant PDF.
    Uses PyQt6 QTextDocument and QPrinter.
    """

    @classmethod
    def generate_pdf(cls, audit_data: Dict[str, Any], output_path: str, chart_base64: Optional[str] = None) -> bool:
        """
        Generates a PDF file at the given path. Returns True on success.
        """
        try:
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtGui import QTextDocument, QPageSize, QPageLayout
            from PyQt6.QtPrintSupport import QPrinter
            from PyQt6.QtCore import QSizeF, Qt, QMarginsF
            if QApplication.instance() is None:
                _app = QApplication([])
        except ImportError as e:
            logger.exception('PyQt6 not available for PDF export: %s', e)
            return False
        chart_base64 = chart_base64 or audit_data.get('chart_base64')
        import os
        import base64
        logo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'assets', 'bdbm_logo.png'))
        logo_url = ''
        try:
            if os.path.isfile(logo_path):
                with open(logo_path, 'rb') as f:
                    logo_b64 = base64.b64encode(f.read()).decode('utf-8')
                    logo_url = f'data:image/png;base64,{logo_b64}'
            else:
                logger.debug('Logo file not found: %s', logo_path)
        except Exception as e:
            logger.debug('Error loading logo: %s', e)
        html = cls._build_html(audit_data, chart_base64, logo_url)
        doc = QTextDocument()
        doc.setHtml(html)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(output_path)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        layout = QPageLayout()
        layout.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        layout.setOrientation(QPageLayout.Orientation.Portrait)
        layout.setMargins(QMarginsF(15, 15, 15, 15))
        printer.setPageLayout(layout)
        doc.print(printer)
        logger.info('Cognitive audit PDF generated: %s', output_path)
        return True

    @classmethod
    def _build_html(cls, data: Dict[str, Any], chart_base64: Optional[str] = None, logo_url: str = '') -> str:
        vm = data.get('vital_metrics', {})
        domains = data.get('domain_clusters', [])
        net = data.get('network_metrics', {})
        kin = data.get('kinetics_metrics', {})
        anchors = data.get('top_anchors', [])
        ts = data.get('timestamp', datetime.datetime.now().strftime('%Y-%m-%d'))
        html = ''.join(["""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {
        font-family: 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif;
        color: #1e293b;
        margin: 0;
        padding: 0;
        line-height: 1.45;
        font-size: 11pt;
    }
    .header {
        border-bottom: 3px solid #0ea5e9;
        padding-bottom: 12px;
        margin-bottom: 24px;
    }
    .header-title {
        font-size: 20pt;
        font-weight: bold;
        color: #0f172a;
        margin: 0;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .header-subtitle {
        font-size: 11pt;
        font-weight: 600;
        color: #0ea5e9;
        margin-top: 4px;
    }
    .badge {
        background-color: #f1f5f9;
        color: #475569;
        font-size: 9.5pt;
        padding: 4px 10px;
        border-radius: 4px;
        border: 1px solid #cbd5e1;
        display: inline-block;
        font-weight: bold;
    }
    .section-title {
        font-size: 13pt;
        font-weight: bold;
        color: #0f172a;
        border-left: 4px solid #0ea5e9;
        padding-left: 10px;
        margin-top: 24px;
        margin-bottom: 12px;
        background-color: #f8fafc;
        padding-top: 4px;
        padding-bottom: 4px;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 8px;
        margin-bottom: 16px;
    }
    th, td {
        padding: 8px 10px;
        text-align: left;
        border-bottom: 1px solid #e2e8f0;
        font-size: 10pt;
    }
    th {
        background-color: #f1f5f9;
        color: #334155;
        font-weight: 600;
        border-top: 1px solid #cbd5e1;
    }
    tr:nth-child(even) {
        background-color: #fdfdfd;
    }
    .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
    }
    .stat-box {
        background-color: #f8fafc;
        border: 1px solid #cbd5e1;
        border-radius: 6px;
        padding: 12px;
        margin-bottom: 16px;
    }
    .stat-title {
        font-size: 9.5pt;
        color: #64748b;
        text-transform: uppercase;
        margin-bottom: 2px;
    }
    .stat-val {
        font-size: 16pt;
        font-weight: bold;
        color: #0f172a;
    }
    .chart-box {
        text-align: center;
        margin-top: 16px;
        margin-bottom: 16px;
        padding: 10px;
        background-color: #fcfcfc;
        border: 1px solid #e2e8f0;
        border-radius: 6px;
    }
    .footer {
        margin-top: 32px;
        padding-top: 12px;
        border-top: 1px solid #cbd5e1;
        font-size: 8.5pt;
        color: #64748b;
        text-align: center;
    }
</style>
</head>
<body>

<div class="header">
    <table style="border: none; margin: 0; width: 100%;">
        <tr style="background: transparent;">
            """,
                        (f'<td style="border: none; padding: 0; padding-right: 15px; width: 1%; vertical-align: middle;">\n                <img src="{logo_url}" height="60" />\n            </td>' if logo_url else f'{""}'),
                        '\n            <td style="border: none; padding: 0; vertical-align: middle;">\n                <div class="header-title">',
                        f'{T("report.title")}',
                        '</div>\n                <div class="header-subtitle">',
                        f'{T("report.subtitle")}',
                        '</div>\n            </td>\n            <td style="border: none; padding: 0; text-align: right; vertical-align: top; width: 1%; white-space: nowrap;">\n                <span class="badge">',
                        f'{T("report.badge")}',
                        '</span>\n            </td>\n        </tr>\n    </table>\n</div>\n\n<!-- 1. COGNITIVE PASSPORT -->\n<div class="section-title">',
                        f'{T("report.sec1_title")}',
                        '</div>\n<table style="border: 1px solid #cbd5e1;">\n    <tr>\n        <th>',
                        f'{T("report.sec1_th_param")}',
                        '</th>\n        <th class="num">',
                        f'{T("report.sec1_th_val")}',
                        '</th>\n        <th>',
                        f'{T("report.sec1_th_desc")}',
                        '</th>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_ltm_param")}',
                        '</td>\n        <td class="num"><b>',
                        f'{vm.get("ltm_count", 0)}',
                        '</b></td>\n        <td>',
                        f'{T("report.sec1_ltm_desc")}',
                        '</td>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_stm_param")}',
                        '</td>\n        <td class="num">',
                        f'{vm.get("stm_count", 0)}',
                        '</td>\n        <td>',
                        f'{T("report.sec1_stm_desc")}',
                        '</td>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_ratio_param")}',
                        '</td>\n        <td class="num"><b>',
                        f'{vm.get("consolidation_ratio", 0):.1%}',
                        '</b></td>\n        <td>',
                        f'{T("report.sec1_ratio_desc")}',
                        '</td>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_int_param")}',
                        '</td>\n        <td class="num">',
                        f'{vm.get("avg_intensity", 0):.2f}',
                        '</td>\n        <td>',
                        f'{T("report.sec1_int_desc")}',
                        '</td>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_acc_param")}',
                        '</td>\n        <td class="num">',
                        f'{vm.get("avg_access_count", 0):.1f}',
                        ' ×</td>\n        <td>',
                        f'{T("report.sec1_acc_desc")}',
                        '</td>\n    </tr>\n    <tr>\n        <td>',
                        f'{T("report.sec1_ops_param")}',
                        '</td>\n        <td class="num">',
                        f'{vm.get("read_count", 0)}',
                        ' / ',
                        f'{vm.get("write_count", 0)}',
                        ' / ',
                        f'{vm.get("consolidation_count", 0)}',
                        '</td>\n        <td>',
                        f'{T("report.sec1_ops_desc")}',
                        '</td>\n    </tr>\n</table>\n\n<!-- 2. KNOWLEDGE DOMAINS & TAXONOMY -->\n<div class="section-title">',
                        f'{T("report.sec2_title")}',
                        '</div>\n<p style="font-size: 9.5pt; color: #475569; margin-bottom: 8px;">\n',
                        f'{T("report.sec2_desc")}',
                        '\n</p>\n<table>\n    <tr>\n        <th style="width: 12%;">',
                        f'{T("report.sec2_th_domain")}',
                        '</th>\n        <th style="width: 50%;">',
                        f'{T("report.sec2_th_keywords")}',
                        '</th>\n        <th class="num" style="width: 18%;">',
                        f'{T("report.sec2_th_share")}',
                        '</th>\n        <th class="num" style="width: 20%;">',
                        f'{T("report.sec2_th_int")}',
                        '</th>\n    </tr>'])
        if domains:
            for idx, d in enumerate(domains, 1):
                html += f'\n    <tr>\n        <td><b>{T("report.sec2_domain_idx").format(idx)}</b></td>\n        <td>{d.get("keywords", "—")}</td>\n        <td class="num"><b>{d.get("share", 0):.1f} %</b> ({T("report.sec2_centers_count").format(d.get("size", 0))})</td>\n        <td class="num">{d.get("avg_intensity", 0):.2f}</td>\n    </tr>'
        else:
            html += f'\n    <tr><td colspan="4" style="text-align: center; color: #64748b;">{T("report.sec2_empty")}</td></tr>'
        html += f'''\n</table>\n\n<!-- 3. MEMORY TOPOLOGY & KINETICS -->\n<div class="section-title">{T("report.sec3_title", default='3. Memory Topology & Kinetics')}</div>\n<table>\n    <tr>\n        <th style="width: 40%;">{T("report.sec3_th_metric", default='Metric')}</th>\n        <th style="width: 60%;">{T("report.sec3_th_state", default='Value & interpretation')}</th>\n    </tr>\n    <tr>\n        <td>{T("report.sec3_density_param", default='<b>Semantic Network Density (Graph Density)</b><br><span style="font-size: 8.5pt; color: #64748b;">Cosine similarity threshold &ge; 0.55</span>')}</td>\n        <td><span style="font-size: 13pt; font-weight: bold; color: #0284c7;">{net.get("density", 0):.1%}</span> &mdash; <b>{net.get("density_label", "—")}</b></td>\n    </tr>\n    <tr>\n        <td>{T("report.clustering", default='<b>Clustering coefficient (Clustering)</b><br><span style="font-size: 8.5pt; color: #64748b;">Measure of closure of cognitive bubbles</span>')}</td>\n        <td><span style="font-size: 13pt; font-weight: bold; color: #16a34a;">{net.get("clustering", 0):.3f}</span></td>\n    </tr>\n    <tr>\n        <td>{T("report.drift", default='<b>Semantic drift (Centroid Shift)</b><br><span style="font-size: 8.5pt; color: #64748b;">Spatiotemporal deformation of the vector space</span>')}</td>\n        <td><span style="font-size: 13pt; font-weight: bold; color: #9333ea;">{kin.get("drift", 0):.4f}</span> &mdash; <b>{kin.get("status", "—")}</b></td>\n    </tr>\n</table>'''
        eigen_hubs = net.get('eigen_hubs', [])
        if eigen_hubs:
            html += f'''\n<p style="font-size: 9.5pt; color: #475569; margin-top: 12px; margin-bottom: 6px;"><b>{T("report.eigenvector", default='Most significant nodes - Vector centrality (grey eminences):')}</b></p>\n<table>\n    <tr>\n        <th style="width: 15%;">{T("report.sec3_th_id")}</th>\n        <th class="num" style="width: 20%;">{T("report.sec3_th_score", default='Centrality score')}</th>\n        <th style="width: 65%;">{T("report.sec3_th_preview")}</th>\n    </tr>'''
            for h in eigen_hubs:
                html += f'\n    <tr>\n        <td><b>#{h.get("center_id", 0)}</b></td>\n        <td class="num">{h.get("score", 0):.3f}</td>\n        <td>{h.get("text", "")}</td>\n    </tr>'
            html += '\n</table>'
        if chart_base64:
            html += f'''\n<div class="section-title">{T("report.sec4_title")}</div>\n<div class="chart-box">\n    <center>\n        <img src="data:image/png;base64,{chart_base64}" width="560" />\n    </center>\n    <div style="font-size: 8.5pt; color: #64748b; margin-top: 8px; text-align: center;">{T("report.sec4_caption")}</div>\n</div>'''
        section_num = '5' if chart_base64 else '4'
        html += f'''\n<div class="section-title">{T("report.sec5_title").format(section_num)}</div>\n<p style="font-size: 9.5pt; color: #475569; margin-bottom: 8px;">\n{T("report.sec5_desc")}\n</p>\n<table>\n    <tr>\n        <th style="width: 8%;">{T("report.sec5_th_rank")}</th>\n        <th style="width: 12%;">{T("report.sec5_th_id")}</th>\n        <th class="num" style="width: 18%;">{T("report.sec5_th_score")}</th>\n        <th style="width: 62%;">{T("report.sec5_th_text")}</th>\n    </tr>'''
        if anchors:
            for idx, a in enumerate(anchors, 1):
                html += f'\n    <tr>\n        <td><b>#{idx}</b></td>\n        <td>{T("report.center_id").format(a.get("center_id", 0))}</td>\n        <td class="num">{a.get("intensity", 0):.2f} / {a.get("access_count", 0)} ×</td>\n        <td>{a.get("text", "")}</td>\n    </tr>'
        else:
            html += f'\n    <tr><td colspan="4" style="text-align: center; color: #64748b;">{T("report.sec5_empty")}</td></tr>'
        html += f'\n</table>\n\n<div class="footer">\n    {T("report.footer").format(ts)}\n</div>\n\n</body>\n</html>'
        return html
