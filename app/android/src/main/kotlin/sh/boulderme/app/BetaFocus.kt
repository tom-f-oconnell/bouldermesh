package sh.boulderme.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import sh.boulderme.core.BetaDrafts
import sh.boulderme.core.Focus
import sh.boulderme.core.Plan

/**
 * Before a capture of holds in detail: which of the mesh's sets of beta it's for (any of them), and then which of their
 * holds, each ticked to begin with, to leave out. `start` gets what was chosen (BetaDrafts.focused() makes it one set).
 */
@Composable
fun FocusDialog(plan: Plan, dismiss: () -> Unit, start: (Focus) -> Unit) {
    val sets = plan.setsWithHolds
    var chosen by remember { mutableStateOf(if (sets.size == 1) setOf(sets[0].id) else emptySet()) }
    var left by remember { mutableStateOf(emptySet<String>()) }
    val byId = plan.targets.associateBy { it.id }
    val holds = sets.filter { it.id in chosen }.flatMap { it.targets }.distinct().mapNotNull { byId[it] }
    val n = holds.count { it.id !in left }
    AlertDialog(onDismissRequest = dismiss, title = { Text("Hold closeups") },
        text = {
            Column(Modifier.heightIn(max = 520.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("Sets of beta to photograph the holds of:", style = MaterialTheme.typography.bodySmall)
                for (s in sets) Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = s.id in chosen, onCheckedChange = { on -> chosen = if (on) chosen + s.id else chosen - s.id })
                    Text("${s.name ?: "A set of beta"} (${s.targets.size} hold${if (s.targets.size == 1) "" else "s"})" +
                        if (s.id.startsWith(BetaDrafts.LOCAL) || plan.targets.any { it.id.startsWith("d-${s.id}-") }) " · not sent yet" else "")
                }
                if (holds.isNotEmpty()) Text("Its holds (untick any to leave out):", style = MaterialTheme.typography.bodySmall, modifier = Modifier.heightIn(min = 24.dp))
                for ((i, t) in holds.withIndex()) Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = t.id !in left, onCheckedChange = { on -> left = if (on) left - t.id else left + t.id })
                    val c = plan.roleColor(t.role)
                    Text("●", color = c?.let { Color(it[0], it[1], it[2]) } ?: Color.Gray, fontWeight = FontWeight.Bold)
                    Text(" ${i + 1}. " + BetaDrafts.label(plan, t, chosen))
                }
            }
        },
        confirmButton = { TextButton(enabled = n > 0, onClick = { start(Focus(sets.map { it.id }.filter { it in chosen }, left.filter { l -> holds.any { it.id == l } })) }) {
            Text(if (n > 0) "Capture $n hold${if (n == 1) "" else "s"}" else "Choose some holds") } },
        dismissButton = { TextButton(onClick = dismiss) { Text("Cancel") } })
}
