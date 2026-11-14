typedef struct {
    int x;
    struct {
        int inner_a;
        char inner_b;
    } nested;
    void (*handler)(int);
    int (*op)(void);
} Edge;

struct Outer {
    Edge e;
    const char *label;
};
