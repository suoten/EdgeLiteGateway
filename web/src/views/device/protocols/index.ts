/**
 * Protocol form/detail component registry.
 *
 * Provides dynamic component resolution based on device protocol type.
 * Each protocol can have a custom form component for create/edit and
 * a custom detail component for the device detail page.
 *
 * If no custom component exists for a protocol, null is returned and
 * the caller falls back to the generic protocol config form built from
 * PROTOCOL_CONFIGS.
 */
import { defineComponent, h, type Component } from 'vue'
import { NForm, NFormItem, NInput, NInputNumber, NSelect, NSwitch, NDivider, NText, type FormInst } from 'naive-ui'
import { PROTOCOL_CONFIGS } from '@/constants/protocolConfig'
import type { ProtocolFieldDef } from '@/constants/protocolConfig'

/**
 * Generic protocol form component that renders fields based on PROTOCOL_CONFIGS.
 * Used as fallback when no protocol-specific component exists.
 */
const GenericProtocolForm = defineComponent({
  name: 'GenericProtocolForm',
  props: {
    protocol: { type: String, required: true },
    modelValue: { type: Object, default: () => ({}) },
    disabled: { type: Boolean, default: false },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const configs = PROTOCOL_CONFIGS
    const updateField = (key: string, value: any) => {
      const newData = { ...props.modelValue, [key]: value }
      emit('update:modelValue', newData)
    }
    return () => {
      const cfg = configs.value[props.protocol]
      if (!cfg) return null
      return h('div', { class: 'protocol-form-generic' }, [
        h(NDivider, { titlePlacement: 'left' }, { default: () => cfg.label }),
        ...cfg.configFields.map((field: ProtocolFieldDef) => {
          if (field.notImplemented) {
            return h(NFormItem, { label: field.label }, {
              default: () => h(NText, { depth: 3, italic: true }, { default: () => field.tooltip || 'Not implemented' })
            })
          }
          const value = props.modelValue?.[field.key] ?? field.default
          if (field.type === 'number') {
            return h(NFormItem, { label: field.label, required: field.required }, {
              default: () => h(NInputNumber, {
                value,
                disabled: props.disabled || field.notImplemented,
                placeholder: field.placeholder,
                min: field.min,
                max: field.max,
                'onUpdate:value': (v: number | null) => updateField(field.key, v),
              })
            })
          }
          if (field.type === 'boolean') {
            return h(NFormItem, { label: field.label }, {
              default: () => h(NSwitch, {
                value,
                disabled: props.disabled,
                'onUpdate:value': (v: boolean) => updateField(field.key, v),
              })
            })
          }
          if (field.type === 'select') {
            return h(NFormItem, { label: field.label, required: field.required }, {
              default: () => h(NSelect, {
                value,
                disabled: props.disabled,
                options: field.options || [],
                'onUpdate:value': (v: any) => updateField(field.key, v),
              })
            })
          }
          if (field.type === 'password') {
            return h(NFormItem, { label: field.label, required: field.required }, {
              default: () => h(NInput, {
                value,
                type: 'password',
                showPasswordOn: 'click',
                disabled: props.disabled,
                placeholder: field.placeholder,
                'onUpdate:value': (v: string) => updateField(field.key, v),
              })
            })
          }
          return h(NFormItem, { label: field.label, required: field.required }, {
            default: () => h(NInput, {
              value,
              disabled: props.disabled,
              placeholder: field.placeholder,
              'onUpdate:value': (v: string) => updateField(field.key, v),
            })
          })
        }),
      ])
    }
  },
})

/**
 * Generic protocol detail component for the device detail page.
 */
const GenericProtocolDetail = defineComponent({
  name: 'GenericProtocolDetail',
  props: {
    protocol: { type: String, required: true },
    config: { type: Object, default: () => ({}) },
    device: { type: Object, default: () => ({}) },
  },
  setup(props) {
    const configs = PROTOCOL_CONFIGS
    return () => {
      const cfg = configs.value[props.protocol]
      if (!cfg) return null
      return h('div', { class: 'protocol-detail-generic' }, [
        h(NDivider, { titlePlacement: 'left' }, { default: () => cfg.label }),
        ...cfg.configFields.map((field: ProtocolFieldDef) => {
          const value = props.config?.[field.key] ?? field.default
          return h('div', { key: field.key, style: 'display:flex;justify-content:space-between;padding:4px 0;' }, [
            h('span', { style: 'color:var(--text-color-3);' }, field.label + ':'),
            h('span', null, String(value ?? '-')),
          ])
        }),
      ])
    }
  },
})

// Registry of protocol-specific components (can be extended)
const _formRegistry: Record<string, Component> = {}
const _detailRegistry: Record<string, Component> = {}

/**
 * Get the protocol-specific form component for create/edit dialogs.
 * Returns null if no specific component exists (caller should use generic form).
 */
export function getProtocolFormComponent(protocol: string): Component | null {
  return _formRegistry[protocol] || GenericProtocolForm
}

/**
 * Get the protocol-specific detail component for the device detail page.
 * Returns null if no specific component exists (caller should use generic detail).
 */
export function getProtocolDetailComponent(protocol: string): Component | null {
  return _detailRegistry[protocol] || GenericProtocolDetail
}

/**
 * Register a custom protocol form component.
 */
export function registerProtocolFormComponent(protocol: string, component: Component): void {
  _formRegistry[protocol] = component
}

/**
 * Register a custom protocol detail component.
 */
export function registerProtocolDetailComponent(protocol: string, component: Component): void {
  _detailRegistry[protocol] = component
}
