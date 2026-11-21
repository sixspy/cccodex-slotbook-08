export const reconcileViewTemplate = `
  <div v-show="tab===4" class="panel">
    <h2>对账互证</h2>
    <p :class="recon.all_ok ? 'ok' : 'err'">{{ recon.all_ok ? '全部同源' : '存在分裂' }}</p>
    <p :class="recon.any_hold_drift ? 'err' : 'ok'">
      {{ recon.any_hold_drift ? '存在同档同客双占位偏航，服务端已拒写待人工处置' : '同档同客占位唯一，无偏航' }}
    </p>
    <table>
      <thead><tr><th>档</th><th>盘面已订</th><th>正式册席</th><th>占位行</th><th>偏航当事人</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="i in (recon.items||[])" :key="i.slot_code">
          <td>{{ i.slot_code }}</td><td>{{ i.slot_booked }}</td>
          <td>{{ i.booking_seats }}</td><td>{{ i.open_hold_rows }}</td>
          <td :class="i.hold_drift ? 'err' : ''">
            {{ i.hold_drift ? i.duplicate_parties.join('、') : '—' }}
          </td>
          <td :class="(i.ok && !i.hold_drift)?'ok':'err'">
            {{ i.hold_drift ? '偏航' : (i.ok ? '通过' : '分裂') }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
`;
