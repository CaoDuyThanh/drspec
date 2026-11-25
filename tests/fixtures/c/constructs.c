/* Conformance file for C read with the C++ grammar: each construct sits between two sentinel functions. */

/* ---- compound_literal ---- */
int before_compound_literal(void) { return 0; }

typedef struct { int r, g, b; } Col;
Col make_col(void) { Col c = (Col){ 1, 2, 3 }; return c; }

int after_compound_literal(void) { return 0; }

/* ---- designated_init ---- */
int before_designated_init(void) { return 1; }

struct P { int x, y; };
struct P make_p(void) { struct P p = { .y = 2, .x = 1 }; return p; }

int after_designated_init(void) { return 1; }

/* ---- static_inline ---- */
int before_static_inline(void) { return 2; }

static inline int twice(int a) { return a * 2; }

int after_static_inline(void) { return 2; }

/* ---- restrict_pointers ---- */
int before_restrict_pointers(void) { return 3; }

void copy_n(char *restrict dst, const char *restrict src, int n) { while (n--) *dst++ = *src++; }

int after_restrict_pointers(void) { return 3; }

/* ---- api_macro ---- */
int before_api_macro(void) { return 4; }

#define RLAPI
RLAPI int api_fn(int a) { return a; }

int after_api_macro(void) { return 4; }

/* ---- statement_expression ---- */
int before_statement_expression(void) { return 5; }

int maxi(int a, int b) { return ({ int _a = a, _b = b; _a > _b ? _a : _b; }); }

int after_statement_expression(void) { return 5; }

/* ---- bit_fields ---- */
int before_bit_fields(void) { return 6; }

struct Flags { unsigned a : 1; unsigned b : 3; };
int flag_a(struct Flags f) { return f.a; }

int after_bit_fields(void) { return 6; }

/* ---- variadic ---- */
int before_variadic(void) { return 7; }

#include <stdarg.h>
int sum_n(int n, ...) { va_list ap; va_start(ap, n); int s = 0; while (n--) s += va_arg(ap, int); va_end(ap); return s; }

int after_variadic(void) { return 7; }

/* ---- member_comparison ---- */
int before_member_comparison(void) { return 8; }

struct S { int m; };
int lt(struct S x, int B) { return x.m < B; }

int after_member_comparison(void) { return 8; }

/* ---- goto_label ---- */
int before_goto_label(void) { return 9; }

int with_goto(int a) { if (a) goto done; a = 1; done: return a; }

int after_goto_label(void) { return 9; }

/* ---- generic_selection ---- */
int before_generic_selection(void) { return 10; }

#define kind(x) _Generic((x), int: 1, float: 2, default: 0)
int kind_of(int v) { return kind(v); }

int after_generic_selection(void) { return 10; }

/* ---- inline_asm ---- */
int before_inline_asm(void) { return 11; }

static inline void barrier(void) { __asm__ __volatile__("" ::: "memory"); }

int after_inline_asm(void) { return 11; }

/* ---- function_pointer_table ---- */
int before_function_pointer_table(void) { return 12; }

static int add1(int a) { return a + 1; }
static int (*table[])(int) = { add1 };
int call_table(int i, int v) { return table[i](v); }

int after_function_pointer_table(void) { return 12; }

/* ---- cpp_keywords_as_names ---- */
int before_cpp_keywords_as_names(void) { return 13; }

int class_of(int new, int delete) { int this = new + delete; int template = this; return template; }

int after_cpp_keywords_as_names(void) { return 13; }

/* ---- kr_definition ---- */
int before_kr_definition(void) { return 14; }

int kr(a, b)
    int a;
    int b;
{
    return a + b;
}

int after_kr_definition(void) { return 14; }

/* ---- function_returning_function_pointer ---- */
int before_function_returning_function_pointer(void) { return 15; }

int (*pick2(void))(int) { return 0; }

int after_function_returning_function_pointer(void) { return 15; }

/* ---- typeof_operator ---- */
int before_typeof_operator(void) { return 16; }

#define swap_t(a, b) do { __typeof__(a) t = (a); (a) = (b); (b) = t; } while (0)
void swap_two(int *a, int *b) { swap_t(*a, *b); }

int after_typeof_operator(void) { return 16; }

/* ---- variable_length_array ---- */
int before_variable_length_array(void) { return 17; }

int vla_sum(int n) { int v[n]; int s = 0; for (int i = 0; i < n; i++) { v[i] = i; s += v[i]; } return s; }

int after_variable_length_array(void) { return 17; }

/* ---- flexible_array_member ---- */
int before_flexible_array_member(void) { return 18; }

struct Buf { int len; char data[]; };
int buf_len(struct Buf *b) { return b->len; }

int after_flexible_array_member(void) { return 18; }

/* ---- bool_type ---- */
int before_bool_type(void) { return 19; }

#include <stdbool.h>
_Bool is_zero(int a) { return a == 0; }
bool is_one(int a) { return a == 1; }

int after_bool_type(void) { return 19; }

/* ---- x_macro ---- */
int before_x_macro(void) { return 20; }

#define COLORS X(RED) X(GREEN) X(BLUE)
enum Color {
#define X(c) c,
    COLORS
#undef X
};
int color_count(void) {
#define X(c) +1
    return 0 COLORS;
#undef X
}

int after_x_macro(void) { return 20; }

/* ---- function_defined_by_macro ---- */
int before_function_defined_by_macro(void) { return 21; }

#define DEFINE_GETTER(name, field) int get_##name(struct G *g) { return g->field; }
struct G { int w; };
DEFINE_GETTER(w, w)
int after_getter(void) { return 1; }

int after_function_defined_by_macro(void) { return 21; }

/* ---- array_parameter_static ---- */
int before_array_parameter_static(void) { return 22; }

int first(int a[static 1]) { return a[0]; }

int after_array_parameter_static(void) { return 22; }

/* ---- anonymous_union_struct ---- */
int before_anonymous_union_struct(void) { return 23; }

struct V { union { int i; float f; }; };
int v_int(struct V v) { return v.i; }

int after_anonymous_union_struct(void) { return 23; }

/* ---- enum_function_pointer_typedef ---- */
int before_enum_function_pointer_typedef(void) { return 24; }

typedef int (*Handler)(int);
Handler get_handler(void) { return 0; }
int run_handler(Handler h, int v) { return h(v); }

int after_enum_function_pointer_typedef(void) { return 24; }
